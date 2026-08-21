"""Benchmark A — exact evidence replay vs. explicit re-evaluation (v0.2.x Slice 2).

Execute a mission against evidence snapshot A; mutate the evidence to B; restart (fresh runtime, same
on-disk log). Exact replay (rehydrate) must resolve A — the same sealed intent, the same ContextEpoch,
and reproduce the original plan fingerprint — even though the source is now B. Explicit re-evaluation
resolves B, producing a new epoch, with the A→B difference recorded explainably. Replay never re-fires
side effects: rehydrate reconstructs/verifies the decision path; run() executes under the normal gates.
"""
from __future__ import annotations

import pytest

from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor
from agentic_os.mission.runtime import MissionRuntime, ReplayError, ReplayDivergence
from agentic_os.mission.store import EventStore
from agentic_os.mission.context_view import epoch_from_refs

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]

INTENT_A = {"content_hash": "sha256:A", "produced_by": "discovery@0.2.0",
            "evidence": [{"field": "acct", "source_ref": "crm://42#v1"}]}
INTENT_B = {"content_hash": "sha256:B", "produced_by": "discovery@0.2.0",
            "evidence": [{"field": "acct", "source_ref": "crm://42#v2"}]}   # same source, new revision


def _runtime(store):
    reg, client = build_fleet()
    return MissionRuntime(reg, Executor(client), store=store)


def _plan_meta(rt, mid):
    return next(e for e in rt.store.for_mission(mid) if e.type == "PlanCreated").payload


def test_exact_replay_resolves_A_and_reproduces_the_sealed_plan(tmp_path):
    path = str(tmp_path / "events.jsonl")
    rt = _runtime(EventStore(path=path))
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding",
                          verified_intent=INTENT_A)
    mid = m.id
    sealed = _plan_meta(rt, mid)
    epoch_A = sealed["context_epoch_id"]
    fp_A = sealed["plan_fingerprint"]
    assert epoch_A == epoch_from_refs(["acct:crm://42#v1"], pins=["sha256:A"]).id

    # ---- evidence mutates A → B in the outside world (the mission's log is unchanged) ----
    # ---- crash + restart: fresh runtime, same on-disk event log ----
    rt2 = _runtime(EventStore(path=path))
    m2 = rt2.rehydrate(mid)                                    # EXACT REPLAY
    assert m2.intent_content_hash == "sha256:A"               # resolves A, not B
    assert m2.evidence_refs == ["acct:crm://42#v1"]
    assert m2.context_epoch_id == epoch_A                     # same pinned evidence view
    # reproduced the sealed plan (no ReplayError raised) and re-derives the same fingerprint
    assert _plan_meta(rt2, mid)["plan_fingerprint"] == fp_A


def test_re_evaluation_resolves_B_and_records_the_difference(tmp_path):
    path = str(tmp_path / "events.jsonl")
    rt = _runtime(EventStore(path=path))
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding",
                          verified_intent=INTENT_A)
    mid = m.id
    epoch_A = _plan_meta(rt, mid)["context_epoch_id"]

    # explicit re-evaluation against the re-sealed intent B (Discovery re-ran on the new evidence)
    m2 = rt.re_evaluate(mid, verified_intent=INTENT_B, cause="source revised v1→v2")
    assert m2.intent_content_hash == "sha256:B"               # resolves B
    assert m2.evidence_refs == ["acct:crm://42#v2"]
    epoch_B = m2.context_epoch_id
    assert epoch_B != epoch_A                                 # a different pinned evidence view

    # the A→B difference is recorded explainably
    ev = next(e for e in rt.store.for_mission(mid) if e.type == "PlanReevaluated").payload
    assert ev["evidence_changed"] is True
    assert ev["old_context_epoch_id"] == epoch_A and ev["new_context_epoch_id"] == epoch_B
    assert ev["cause"] == "source revised v1→v2"


def test_replay_fails_closed_when_the_plan_cannot_be_reproduced(tmp_path):
    path = str(tmp_path / "events.jsonl")
    rt = _runtime(EventStore(path=path))
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding",
                          verified_intent=INTENT_A)
    mid = m.id

    # simulate plan drift (templates/registry changed): the recompiled signature no longer matches.
    # Fails closed — the signature check raises ReplayDivergence, or the fingerprint/epoch check raises
    # ReplayError; either way replay refuses rather than folding events into a divergent program.
    rt2 = _runtime(EventStore(path=path))
    rt2._signature = lambda plan: "compliance.file_consent->DRIFTED"   # type: ignore[assignment]
    with pytest.raises((ReplayError, ReplayDivergence)):
        rt2.rehydrate(mid)
