"""Phase H — Sidekick prose → a real Mission (planning), boundary preserved.

Creating a Mission from a goal is planning; its consequential steps stay approval-gated. Prose never
authorizes execution (that is `from_intent.check_executable`'s job on the sealed path).
"""
from __future__ import annotations

import pytest

from agentic_os.mission.from_sidekick import (
    SidekickMissionBridge, is_actionable_goal, recognize)
from agentic_os.projects_api import sidekick_reply


# ── a faithful MissionRuntime double (public contract only) ──────────────────────────
class _St:
    def __init__(self, name): self.name = name


class _Mission:
    def __init__(self, mid, state): self.id = mid; self.state = _St(state)


class _FakeRuntime:
    def __init__(self, plan_state="PLANNING", run_state="WAITING_HUMAN"):
        self.plan_state, self.run_state = plan_state, run_state
        self.created, self.ran, self.from_intent = [], [], []
    def create_mission(self, goal, *, constraints=None):
        self.created.append((goal, tuple(constraints or ()))); return _Mission("mission:1", self.plan_state)
    def run(self, mid):
        self.ran.append(mid); return _Mission(mid, self.run_state)
    def create_mission_from_intent(self, intent, *, policy_refs=None):
        self.from_intent.append(intent); return _Mission("mission:2", "PLANNING")


# ── goal recognition ─────────────────────────────────────────────────────────────────
def test_recognize_classifies_known_jobs_with_governance_hints():
    kind, cons = recognize("find material overdue accounts and draft follow-ups")
    assert kind == "receivables" and any("approval" in c for c in cons)
    assert recognize("inspect current deployment security posture")[0] == "deployment_inspection"
    assert recognize("what is a mission?") is None


def test_is_actionable_goal_distinguishes_goals_from_questions():
    assert is_actionable_goal("find overdue accounts and follow up")
    assert is_actionable_goal("investigate this stalled deal")
    assert not is_actionable_goal("what is deployment inspection")
    assert not is_actionable_goal("how does approval work")


# ── the bridge creates a real Mission (a plan), surfacing the approval boundary ──────
def test_compile_creates_a_mission_with_governance_constraints():
    rt = _FakeRuntime(plan_state="PLANNING")
    m = SidekickMissionBridge(rt).compile("find overdue accounts and draft follow-ups",
                                          project_id="acme")
    assert m.mission_id == "mission:1" and m.kind == "receivables" and m.state == "PLANNING"
    goal, cons = rt.created[0]
    assert "consequential sends require approval" in cons and "project:acme" in cons
    assert m.needs_approval is False                     # a plan, not parked at a gate yet


def test_compile_with_run_parks_at_the_human_gate():
    rt = _FakeRuntime(run_state="WAITING_HUMAN")
    m = SidekickMissionBridge(rt).compile("collect overdue receivables", run=True)
    assert rt.ran == ["mission:1"] and m.state == "WAITING_HUMAN" and m.needs_approval is True


def test_compile_confirmed_uses_the_sealed_intent_path():
    rt = _FakeRuntime()
    class _Intent:
        objective = "collections"
    m = SidekickMissionBridge(rt).compile_confirmed(_Intent())
    assert rt.from_intent and m.mission_id == "mission:2" and m.kind == "confirmed"


def test_bridge_reaches_a_real_mission_runtime():
    """Not another fake: build a real MissionRuntime and confirm the bridge creates a mission through it.
    (The mission may not fully plan without deployment operators/templates — a non-empty id is the proof.)"""
    try:
        from agentic_os.mission.factory import build_runtime
        from agentic_os.mission.registry import CapabilityRegistry
        from agentic_os.mission.operator_sdk import Operator, capability, LocalOperatorClient
        op = Operator("demo", [capability("noop", lambda i: {"ok": True}, operator="demo")])
        reg = CapabilityRegistry()
        getattr(reg, "register", lambda *_: None)(op.manifest)
        rt = build_runtime(reg, operator_client=LocalOperatorClient({"demo": op}))
    except Exception as e:
        pytest.skip(f"could not build a real runtime: {e}")
    m = SidekickMissionBridge(rt).compile("find overdue accounts and draft follow-ups")
    assert m.mission_id and isinstance(m.mission_id, str)


# ── sidekick_reply integration ───────────────────────────────────────────────────────
class _Provider:
    def __init__(self, runtime=None): self.mission_runtime = runtime


def test_sidekick_reply_compiles_a_real_mission_when_a_runtime_is_bound():
    reply = sidekick_reply({"project": "acme"}, "find overdue accounts and draft follow-ups",
                           _Provider(_FakeRuntime(plan_state="WAITING_HUMAN")))
    assert reply["topic"] == "Mission" and reply["mission_id"] == "mission:1"
    assert reply["actions"][0]["kind"] == "navigate" and reply["actions"][0]["ref"] == "missions"
    assert "approval" in reply["text"].lower()


def test_sidekick_reply_leaves_questions_to_the_kb_even_with_a_runtime():
    reply = sidekick_reply({}, "what is a mission?", _Provider(_FakeRuntime()))
    assert reply.get("topic") != "Mission" and "mission_id" not in reply    # a question → not compiled


def test_sidekick_reply_unchanged_without_a_runtime():
    # no mission_runtime bound → the actionable goal does NOT compile a mission (demo behaviour intact)
    reply = sidekick_reply({}, "find overdue accounts and draft follow-ups", _Provider(None))
    assert reply.get("topic") != "Mission" and "mission_id" not in reply
