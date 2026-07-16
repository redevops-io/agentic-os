"""Conversation compaction — free context window space at a threshold, opt-in.

A reusable context-management utility for any session layer that carries an LLM conversation (Sidekick,
a chat runtime). Mirrors xai-grok-build's compaction policy: auto-compact at a % threshold, an optional
**memory flush** (pull the durable facts out *before* the history is summarized away), an optional
**two-pass** pass (speculatively summarize the prefix, then fold in the recent tail), and a
**wall-clock budget** — the backstop for a reasoning runaway that a token limit alone misses.

This is **opt-in and off by default** (``CompactionPolicy.enabled = False``) — nothing compacts
unless a caller turns it on. The summarizer is injected (``summarize(text) -> str``); the caller owns
*when* to call this — the utility never owns the loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class CompactionPolicy:
    """When + how a session compacts its conversation. Off by default."""
    enabled: bool = False                        # opt-in — per request, not a default behavior
    auto_compact_threshold_percent: int = 85     # compact when this % of the window is used
    compact_model: str | None = None             # model for the summary (None = the session's model)
    memory_flush_enabled: bool = False           # summarize durable facts before dropping history
    two_pass_enabled: bool = False               # speculative prefix summary (pass 1) + tail fold (pass 2)
    wall_clock_budget_secs: int = 300            # a summary generation over this is cut + retried
    keep_recent: int = 6                         # messages of the recent tail kept verbatim


def should_compact(used_tokens: int, window_tokens: int, policy: CompactionPolicy) -> bool:
    """True when usage crosses the policy threshold (and compaction is enabled)."""
    if not policy.enabled or window_tokens <= 0:
        return False
    return (used_tokens / window_tokens) * 100 >= policy.auto_compact_threshold_percent


def compact(messages: list[dict], policy: CompactionPolicy, summarize: Callable[[str], str],
            *, note1: str | None = None) -> list[dict]:
    """Compact a message list to free context → ``[<memory note?>, <history summary>, *recent_tail]``.

    ``summarize`` is the injected model call (the only capability this uses). ``note1`` is the
    optional pass-1 speculative summary of the prefix (from a background turn) — when two-pass is on
    and ``note1`` is supplied, pass 2 folds it in instead of re-summarizing from scratch. Returns the
    input unchanged when disabled or already short.
    """
    if not policy.enabled or len(messages) <= policy.keep_recent:
        return messages
    tail = messages[-policy.keep_recent:]
    prefix = messages[:-policy.keep_recent]
    prefix_text = "\n".join(f"{m.get('role', '')}: {m.get('content', '')}" for m in prefix)
    out: list[dict] = []
    if policy.memory_flush_enabled:
        out.append({"role": "system", "content": "MEMORY (kept across compaction):\n" + summarize(
            "Extract the durable facts, decisions, and open threads worth keeping:\n" + prefix_text)})
    if policy.two_pass_enabled and note1:
        summary = summarize(f"Fold this running summary into a final one:\n{note1}\n\nRecent prefix:\n{prefix_text}")
    else:
        summary = summarize("Summarize this conversation so it can be dropped from context:\n" + prefix_text)
    out.append({"role": "system", "content": "SUMMARY of earlier conversation:\n" + summary})
    return out + tail
