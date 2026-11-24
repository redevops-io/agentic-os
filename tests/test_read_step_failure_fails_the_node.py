"""A read-only step that reports its own failure is a failed step.

Verification covers consequential (side-effecting) nodes. A read-only step answering ``{"status": "error", ...}`` used to be recorded as
NodeSucceeded: its error dict became the world fact of its outcome and every later step ran on it. These tests pin the rule for the three
shapes a read can take: it fails, it succeeds, and a consequential step keeps its own (stricter) path.
"""
from __future__ import annotations

from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient, Operator, capability
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.templates import TEMPLATES
from agentic_os.mission.types import ExecutionIntent, IntentStep


def _two_reads(mission_id: str) -> ExecutionIntent:
    a = IntentStep(outcome="reading", need="read something")
    b = IntentStep(outcome="summary", need="summarise the reading", inputs_from=["reading"])
    return ExecutionIntent(mission_id=mission_id, rationale="two reads", steps=[a, b])


def _run(read, monkeypatch):
    monkeypatch.setitem(TEMPLATES, "two_reads_test", _two_reads)
    seen = []
    op = Operator("reader", [
        capability("reader.read", read, provides=["reading"]),
        capability("reader.summarise", lambda i: seen.append(i.get("reading")) or {"summary": {"ok": True}}, provides=["summary"]),
    ])
    reg = CapabilityRegistry(); reg.register(op.manifest)
    rt = MissionRuntime(reg, Executor(LocalOperatorClient({"reader": op})))
    m = rt.create_mission("read then summarise", template="two_reads_test", policy_refs=["*"])
    rt.run(m.id)
    events = [(e["type"], (e.get("payload") or {}).get("capability"), (e.get("payload") or {})) for e in rt.repo.timeline(m.id)]
    return rt.repo.get(m.id) if hasattr(rt.repo, "get") else None, events, seen


def test_a_read_that_reports_an_error_fails_and_nothing_downstream_runs(monkeypatch):
    _, events, seen = _run(lambda i: {"status": "error", "error": "source_unreachable", "summary": "the page did not answer"}, monkeypatch)
    failed = [p for t, c, p in events if t == "NodeFailed" and c == "reader.read"]
    assert failed and failed[0]["reason"] == "operator_reported_failure" and failed[0]["operator"]["error"] == "source_unreachable"
    assert not any(t == "NodeSucceeded" for t, c, _ in events) and seen == []        # the summary never ran on an error dict


def test_a_read_that_succeeds_still_flows_into_the_next_step(monkeypatch):
    _, events, seen = _run(lambda i: {"reading": {"value": 7}}, monkeypatch)
    assert [c for t, c, _ in events if t == "NodeSucceeded"] == ["reader.read", "reader.summarise"]
    assert seen and seen[0]["reading"] == {"value": 7}
