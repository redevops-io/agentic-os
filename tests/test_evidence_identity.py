"""Slice 1 — the evidence-identity chain survives the Discovery→Mission boundary (and restart).

Before v0.2.x, ``create_mission`` took a bare goal string and dropped any upstream VerifiedIntent seal.
Now a sealed intent's ``content_hash`` + the evidence refs it consumed are carried onto the mission and
into MissionCreated, and ``rehydrate`` restores them — so replay/EXPLAIN can resolve the exact evidence a
decision used. The intent is duck-typed, so Mission stays decoupled from runtime_contracts.
"""
from __future__ import annotations

from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]


def _runtime(store=None):
    reg, client = build_fleet()
    return MissionRuntime(reg, Executor(client), store=store or EventStore())


class _Evidence:
    """A DecisionEvidence-like value (duck-typed)."""
    def __init__(self, field, source_ref):
        self.field = field
        self.source_ref = source_ref


class _Intent:
    """A VerifiedIntent-like sealed object (duck-typed — no runtime_contracts dependency)."""
    def __init__(self, content_hash, evidence, produced_by="discovery@0.2.0"):
        self.content_hash = content_hash
        self.evidence = tuple(evidence)
        self.produced_by = produced_by


def test_mission_created_from_dict_intent_carries_the_seal():
    rt = _runtime()
    vi = {"content_hash": "sha256:abc123", "produced_by": "discovery@0.2.0",
          "evidence": [{"field": "customer", "source_ref": "crm://acct/42#v7"}]}
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding",
                          verified_intent=vi)
    assert m.intent_content_hash == "sha256:abc123"
    assert m.evidence_refs == ["customer:crm://acct/42#v7"]
    # and it is durably recorded in MissionCreated
    created = next(e for e in rt.store.for_mission(m.id) if e.type == "MissionCreated")
    assert created.payload["intent_content_hash"] == "sha256:abc123"
    assert created.payload["evidence_refs"] == ["customer:crm://acct/42#v7"]
    assert created.payload["intent_produced_by"] == "discovery@0.2.0"


def test_duck_typed_intent_object_is_threaded():
    rt = _runtime()
    vi = _Intent("sha256:deadbeef", [_Evidence("plan", "policy://onboard#3"),
                                     _Evidence("", "span:12-18")])
    m = rt.create_mission("Onboard", policy_refs=GRANTS, template="onboarding", verified_intent=vi)
    assert m.intent_content_hash == "sha256:deadbeef"
    assert m.evidence_refs == ["plan:policy://onboard#3", "span:12-18"]


def test_goal_only_mission_is_unchanged():
    rt = _runtime()
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    assert m.intent_content_hash == "" and m.evidence_refs == []
    created = next(e for e in rt.store.for_mission(m.id) if e.type == "MissionCreated")
    assert "intent_content_hash" not in created.payload
    assert "evidence_refs" not in created.payload


def test_restart_preserves_evidence_identity(tmp_path):
    path = str(tmp_path / "events.jsonl")
    rt = _runtime(store=EventStore(path=path))
    vi = {"content_hash": "sha256:cafe", "evidence": [{"field": "x", "source_ref": "doc#v1"}]}
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding",
                          verified_intent=vi)
    mid = m.id

    # crash + restart: fresh runtime, same on-disk log
    rt2 = _runtime(store=EventStore(path=path))
    m2 = rt2.rehydrate(mid)
    assert m2.intent_content_hash == "sha256:cafe"
    assert m2.evidence_refs == ["x:doc#v1"]
    assert m2.state in set(MissionState)
