"""AI/agent security — the ReDevOps runtime as an Edge Sentinel datasource (Phase 9).

The plan: instrument the agentic runtime's own events as SecurityObservations, so an induced unsafe
agent/tool sequence opens a *normal* investigation and **cannot bypass Governance**. This dogfoods Edge
Sentinel on ReDevOps itself.

The boundary is the platform's OWN generic envelope — ``agentic_os.mission.events.RuntimeEvent``
(runtime-event/v10), which is already the single forensic timeline every subsystem (Context / Mission /
Discovery Runtimes, Sidekick, external-agent adapters) emits. Edge Sentinel consumes that contract rather
than a hand-rolled ``RuntimeSecurityEvent`` replica — the security projection is a *view* over the real
event, so if the runtime-event schema moves, this breaks loudly instead of drifting.

Crucially, Edge Sentinel here is an OBSERVER. It turns runtime events into evidence + findings and opens an
AI_SECURITY case; any response goes through the same governed path as everything else (Phase 7). A
``POLICY_DECISION = DENIED`` in the timeline is the runtime's Governance holding — Edge Sentinel records the
*attempt*, it does not (and cannot) re-drive the denied action.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from agentic_os.mission.events import EventType, ResultStatus, RuntimeEvent

from .evidence import (
    CaseStatus,
    CaseType,
    EvidenceArtifact,
    Finding,
    FindingStatus,
    SecurityCase,
    SecurityObservation,
    Severity,
    _now,
)

# Substring markers of prompt-injection / instruction-override attempts (whole-payload scan; deterministic).
_INJECTION_MARKERS = (
    "ignore previous", "ignore all previous", "disregard", "you are now", "system prompt",
    "reveal your", "exfiltrate", "override", "developer mode", "do anything now", "bypass",
    "print your instructions", "leak", "sudo make me",
)

# Each marker matched as a DELIMITED unit, never a raw substring — the convention attack.py already uses
# for scenario tokens, and for the same reason. Measured on the negative controls in evaluation.py: the raw
# substring scan flagged {"cache_bypass": true} and "the layout disregards the aspect ratio" as
# prompt-injection attempts. A marker that fires on ordinary config keys and ordinary English produces
# findings that open cases and train humans to dismiss them, so the list is only usable because it is
# bounded: `_` counts as part of a word (snake_case keys are one token), so `cache_bypass` is not
# `bypass`. What the boundary cannot decide — whether a verb is being USED on the model or merely MENTIONED
# about something — is decided by position and grammar below, not by a longer exempt-word list.
_INJECTION_RES = tuple(re.compile(r"(?<!\w)" + re.escape(m) + r"(?!\w)", re.ASCII)
                       for m in _INJECTION_MARKERS)

#: Keys whose value the model can be induced to read as instruction input. Not a blocklist of bad words:
#: it names WHERE text reaches a model. Anything not recognised is treated as directive — the unknown-field
#: default is detection, so this list can only ever narrow the downgrade, never widen the hole.
_DIRECTIVE_KEYS = frozenset({
    "prompt", "instruction", "instructions", "system", "system_prompt", "system_prompt_patch",
    "message", "messages", "content", "content_parts", "input", "inputs", "query", "text", "user",
    "user_input", "user_message", "human_input", "completion", "output", "response", "body", "payload",
    "args", "arguments", "parameters", "tool_input", "tool_arguments", "data", "value",
})

#: Keys whose value is prose ABOUT something — release notes, labels, changelogs, tickets. Text here is not
#: instruction input, so a marker in it has to earn a finding with grammar instead of getting one for free.
#: Adding to this list is a visible, reviewable act precisely because it lowers what a scan demands.
_DESCRIPTIVE_KEYS = frozenset({
    "summary", "note", "notes", "feature", "features", "title", "subject", "headline", "description",
    "desc", "details", "comment", "comments", "discussion", "changelog", "commit_message", "release_note",
    "label", "labels", "tag", "tags", "category", "name", "display_name", "reason", "why", "purpose",
    "rationale", "doc", "docs", "readme", "filename", "source_file",
})

#: What an injection verb acts ON when it is being used rather than mentioned.
_ATTACK_OBJECT_RX = re.compile(
    r"\b(?:prompt|prompts|instruction|instructions|secret|secrets|credential|credentials|password|"
    r"passwords|token|tokens|database|databases|data|guard|guards|filter|filters|policy|rules?|safety)\b"
    r"|\bapi[\s_-]keys?\b|\bsystem\s+prompt\b", re.ASCII)

#: Second-person / addressee tells: text aimed AT an assistant, wherever it sits.
_ADDRESSEE_RX = re.compile(r"\b(?:you|your|yours|assistant|chatgpt|claude|llm)\b", re.ASCII)

#: How far past the marker to look for the object it acts on (one clause, same line).
_CLAUSE_CHARS = 60


@dataclass(frozen=True)
class InjectionHit:
    """One marker occurrence, where in the payload it happened, and what it earns.

    `position` is the whole argument in one word: `directive` = the text sits where a model reads commands,
    `mention` = it sits in prose about something and carries no attack grammar. Only `finding_worthy` hits
    open a case; a mention still lands in the forensic timeline, because the event was ingested as an
    evidence-backed observation before any of this ran.
    """
    marker: str
    path: tuple[str, ...]
    position: str
    finding_worthy: bool

    def record(self) -> dict:
        return {"marker": self.marker, "path": "$." + ".".join(self.path),
                "position": self.position, "finding_worthy": self.finding_worthy}


def _iter_strings(node, path: tuple[str, ...] = ()):
    """(path, keys-ancestor-chain, text) for every string in a payload, structurally. Paths matter: the
    same sentence in `prompt` and in `summary` is two different security facts, and a flat blob scan is
    blind to that — which is exactly how "memory leak" became a prompt-injection finding."""
    if isinstance(node, str):
        yield path, tuple(k for k in path if not k.startswith("[")), node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_strings(value, path + (str(key).lower(),))
    elif isinstance(node, (list, tuple)):
        for i, value in enumerate(node):
            yield from _iter_strings(value, path + (f"[{i}]",))


def _position(ancestors: tuple[str, ...]) -> str:
    """The OUTERMOST recognised classification wins. A descriptive key nested inside `messages[].content`
    is still model-reachable, so `content` decides it; a directive key inside `summary` does not get
    rescued by its parent."""
    if any(k in _DIRECTIVE_KEYS for k in ancestors):
        return "directive"
    if any(k in _DESCRIPTIVE_KEYS for k in ancestors):
        return "mention"
    return "directive"                       # unrecognised field: attacker-reachable until proven otherwise


def _attack_grammar(text: str, rx: re.Pattern) -> bool:
    """In prose, a verb needs its object: *leak the system prompt*, *override the guardrails*. Look at the
    clause after the marker, or at whether the text is addressed to a model at all."""
    match = rx.search(text)
    if match is None:
        return False
    tail = text[match.end():match.end() + _CLAUSE_CHARS].split("\n")[0]
    return bool(_ATTACK_OBJECT_RX.search(tail) or _ADDRESSEE_RX.search(text))


def injection_hits(payload) -> tuple[InjectionHit, ...]:
    """Every marker occurrence in a payload, with its position and whether it earns a finding. Deterministic,
    no model in the loop, and the single source of truth for `evaluation._marker_tags` — the measurement must
    not keep its own copy of the match."""
    hits: list[InjectionHit] = []
    for path, ancestors, text in _iter_strings(payload):
        low, position = text.lower(), _position(ancestors)
        for marker, rx in zip(_INJECTION_MARKERS, _INJECTION_RES):
            if not rx.search(low):
                continue
            worthy = True if position == "directive" else _attack_grammar(low, rx)
            hits.append(InjectionHit(marker=marker, path=path, position=position, finding_worthy=worthy))
    return tuple(hits)


def _as_payload(payload):
    """Accept a mapping, a list, or the JSON text of either. A payload that is not JSON at all is treated as
    bare text — the directive reading, because refusing to parse is not a licence to stop looking."""
    if isinstance(payload, (dict, list, tuple)):
        return payload
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            return payload
    return payload


def security_projection(evt: RuntimeEvent) -> dict:
    """The security-relevant view of a runtime event (a projection, not a copy of the whole envelope)."""
    return {
        "actor": evt.actor,
        "event_type": evt.event_type.value,
        "capability_id": evt.capability_id,
        "result_status": evt.result_status.value,
        "mission_id": evt.mission_id,
        "source_runtime": evt.source_runtime or "runtime",
        "policy_context": evt.policy_context,
    }


def _has_injection(payload: dict) -> bool:
    """Does this payload carry an injection attempt rather than a word that looks like one? See
    `injection_hits`: markers in instruction-reachable fields fire as before; markers in prose about
    something have to show attack grammar to earn a finding."""
    return any(h.finding_worthy for h in injection_hits(_as_payload(payload)))


def observation_from_runtime_event(store, evt: RuntimeEvent) -> SecurityObservation:
    """Instrument one runtime event as an immutable-evidence-backed SecurityObservation."""
    raw = evt.to_ndjson()          # the runtime-event/v10 canonical serialization (sorted-key JSON string)
    ev = store.put_evidence(
        EvidenceArtifact.of_raw("runtime_event", evt.source_runtime or "runtime", raw,
                                observed_at=str(evt.timestamp)), raw)
    return store.put_observation(SecurityObservation(
        source=evt.source_runtime or "runtime", source_type="runtime",
        observed_at=str(evt.timestamp), known_at=ev.known_at, raw_evidence_ref=ev.artifact_id,
        normalized_fields=security_projection(evt),
        identity_refs=(f"actor:{evt.actor}",) if evt.actor else ()))


def ingest_runtime_events(store, events: list[RuntimeEvent]):
    """Return [(event, observation)] — each event instrumented as evidence-backed observation."""
    return [(e, observation_from_runtime_event(store, e)) for e in events]


def assess_ai_security(pairs) -> list[Finding]:
    """Deterministic detection over the instrumented event sequence. No model in the loop.

    Detects: (1) prompt-injection markers in a proposal/invocation; (2) an action ATTEMPTED after a
    POLICY_DECISION denied it — a governance-bypass attempt that governance HELD; (3) a capability that
    was itself DENIED. Each finding links to the triggering event's immutable evidence."""
    findings: list[Finding] = []
    # (actor, capability) pairs that governance denied
    denied_caps = {(e.actor, e.capability_id) for e, _ in pairs
                   if e.event_type is EventType.POLICY_DECISION and e.result_status is ResultStatus.DENIED}

    for evt, obs in pairs:
        ev_ref = obs.raw_evidence_ref
        if evt.event_type in (EventType.TOOL_PROPOSAL, EventType.CAPABILITY_INVOCATION) and _has_injection(evt.payload):
            findings.append(Finding(
                claim=f"Prompt-injection markers in {evt.actor}'s {evt.event_type.value} "
                      f"({evt.capability_id or 'no capability'})",
                confidence=0.7, evidence_refs=(ev_ref,), attack_refs=("T1059",),
                affected_assets=(f"actor:{evt.actor}",),
                recommended_actions=(), status=FindingStatus.SUPPORTED))
        if (evt.event_type is EventType.CAPABILITY_INVOCATION
                and evt.result_status in (ResultStatus.ATTEMPTED, ResultStatus.PROPOSED)
                and (evt.actor, evt.capability_id) in denied_caps):
            findings.append(Finding(
                claim=f"{evt.actor} attempted '{evt.capability_id}' after policy DENIED it — "
                      f"Governance held; recording the bypass attempt",
                confidence=0.9, evidence_refs=(ev_ref,), affected_assets=(f"actor:{evt.actor}",),
                status=FindingStatus.SUPPORTED))
        if evt.result_status is ResultStatus.DENIED and evt.event_type in (
                EventType.CAPABILITY_INVOCATION, EventType.TOOL_RESULT):
            findings.append(Finding(
                claim=f"{evt.actor}'s '{evt.capability_id}' was DENIED by Governance",
                confidence=1.0, evidence_refs=(ev_ref,), affected_assets=(f"actor:{evt.actor}",),
                status=FindingStatus.CONFIRMED))
    return findings


def open_ai_security_case(store, events: list[RuntimeEvent], *, title: str = "",
                          severity: Severity = Severity.HIGH) -> SecurityCase:
    """Open a NORMAL investigation (AI_SECURITY case) from a runtime event sequence: instrument events as
    evidence-backed observations, assess for unsafe patterns, and link everything. No action is taken here
    — a response, if any, goes through the Phase-7 governed path. Returns the case (with no findings the
    case simply reflects a clean sequence)."""
    pairs = ingest_runtime_events(store, events)
    findings = assess_ai_security(pairs)
    for f in findings:
        store.put_finding(f)
    obs_refs = tuple(o.id for _, o in pairs)
    ev_refs = tuple(dict.fromkeys(o.raw_evidence_ref for _, o in pairs))
    case = SecurityCase(
        case_type=CaseType.AI_SECURITY, severity=severity, created_at=_now(), known_at=_now(),
        title=title or "AI/agent security investigation",
        status=CaseStatus.INVESTIGATING if findings else CaseStatus.OPEN,
        observation_refs=obs_refs, evidence_refs=ev_refs,
        finding_refs=tuple(f.finding_id for f in findings))
    return store.put_case(case)
