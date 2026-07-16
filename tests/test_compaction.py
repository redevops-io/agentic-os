"""Conversation compaction — opt-in (off by default); memory flush + summary + tail."""
from __future__ import annotations

from agentic_os.mission.compaction import CompactionPolicy, compact, should_compact


def test_off_by_default():
    p = CompactionPolicy()
    assert p.enabled is False
    msgs = [{"role": "user", "content": str(i)} for i in range(20)]
    assert compact(msgs, p, lambda t: "SUMMARY") == msgs          # disabled → unchanged
    assert should_compact(999, 1000, p) is False                  # disabled → never triggers


def test_threshold_triggers_only_when_enabled():
    on = CompactionPolicy(enabled=True)                            # 85% default
    assert should_compact(90, 100, on) is True
    assert should_compact(80, 100, on) is False


def test_compacts_prefix_keeps_tail_and_flushes_memory():
    p = CompactionPolicy(enabled=True, keep_recent=2, memory_flush_enabled=True)
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    out = compact(msgs, p, lambda t: "S")
    assert out[-2:] == msgs[-2:]                                   # recent tail kept verbatim
    head = " ".join(m["content"] for m in out[:-2])
    assert "MEMORY" in head and "SUMMARY" in head                 # memory-flush + summary both ran
    assert len(out) < len(msgs)                                    # actually freed context


def test_two_pass_folds_note1():
    seen = {}
    def summarize(t):
        seen["text"] = t
        return "FINAL"
    p = CompactionPolicy(enabled=True, keep_recent=1, two_pass_enabled=True)
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(4)]
    out = compact(msgs, p, summarize, note1="running-summary")
    assert "running-summary" in seen["text"]                      # pass-2 folded the pass-1 note
    assert any("FINAL" in m["content"] for m in out)
