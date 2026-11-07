"""AOS-DD: "it DID happen" (``reconcile_node(executed=True)``) is accepted for a write step only after the independent read-back confirms the record.

The base lets any authenticated person mark a node done from evidence they type (including a ``result``). For an AOS-DD write node that is not enough:
the runtime looks the record up in the application itself, runs the independent read-back (``ReadBack`` / the AosddVerifier path) against the plan, and
adopts the APPLICATION'S record, never the caller's result. Otherwise it refuses and nothing is recorded as done. The same path is what
``Runner.reconcile`` uses. The receipt marks a step that was completed without a verified adoption (it cannot happen through the API; the check is
for a log written some other way)."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from aosdd_fakes import Crash  # noqa: E402
from test_aosdd_redispatch_guard import S1_CAP, WHY, direct, node_of, parked, posts  # noqa: E402
from test_aosdd_runtime import ADM, APP, REF, REQ, Env, go, state  # noqa: E402

from agentic_os.integrations.aosdd.reconstruct import UNCERTAIN, reconstruct
from agentic_os.integrations.aosdd.runtime import AosddMissionRuntime
from agentic_os.mission.reconcile import ReconciliationError
from agentic_os.mission.types import MissionState

ASSERTED = {"native_id": "SAL-QTN-2026-99999", "native_ids": {"quotation": "SAL-QTN-2026-99999"}, "note": "typed by a person"}


def claim_done(r, mid, principal, *, result=ASSERTED, reason=WHY, cap=S1_CAP):
    return direct(r, mid, cap, principal, reason=reason, executed=True, evidence={"kind": "provider_record", "reference": "typed by a person"}, result=result)


def nothing_recorded_as_done(r, mid, op, env=None):
    t = [e.type for e in r.svc.store.for_mission(mid)]
    assert state(r, mid) is MissionState.WAITING_RECONCILIATION
    assert "ReconciliationResolved" not in t and not [e for e in r.svc.store.for_mission(mid) if e.type == "NodeSucceeded" and e.payload.get("capability") == S1_CAP]
    assert "AosddOpApplied" not in t and r.svc.oplog.get(op)["state"] in ("inflight", "unknown")      # not applied, not released


# ---- refused: nothing recorded as done -------------------------------------------------------------------------------------------------------------
def test_asserting_a_record_that_does_not_exist_is_refused(tmp_path):
    env = Env(tmp_path)
    r, mid = parked(env, "A1")                                                      # killed before the request: the application holds nothing
    op = r.plan(mid)["steps"][0]["op_id"]
    n = posts(env, "erpnext")
    for principal in (REQ, APP, ADM):
        with pytest.raises(ReconciliationError, match="nothing to adopt"):
            claim_done(r, mid, principal)
    nothing_recorded_as_done(r, mid, op)
    assert posts(env, "erpnext") == n
    assert [e for e in r.svc.store.for_mission(mid) if e.type == "AosddAdoptionRefused"]


def test_asserting_a_native_id_that_is_not_the_record_the_application_holds_is_refused(tmp_path):
    env = Env(tmp_path)
    env.stack.fail("erpnext", "POST", "/api/resource/Quotation", 0, times=1, commit=True)       # the record exists; the reply was lost
    r, mid = go(env, "A2")
    op = r.plan(mid)["steps"][0]["op_id"]
    with pytest.raises(ReconciliationError, match="does not match"):
        claim_done(r, mid, REQ)
    nothing_recorded_as_done(r, mid, op)


def test_a_record_the_independent_read_back_does_not_confirm_is_not_adopted(tmp_path):
    env = Env(tmp_path)
    r, mid = parked(env, "A3")
    st = r.plan(mid)["steps"][0]
    env.stack.quotations["SAL-QTN-LATE"] = {"name": "SAL-QTN-LATE", "title": st["derived"]["title"], "docstatus": 0, "party_name": "somebody else", "grand_total": 1.0}
    with pytest.raises(ReconciliationError, match="did not verify"):
        claim_done(r, mid, REQ, result={"native_id": "SAL-QTN-LATE"})
    nothing_recorded_as_done(r, mid, st["op_id"])


def test_an_unreadable_application_refuses_the_claim(tmp_path):
    env = Env(tmp_path)
    env.stack.fail("erpnext", "POST", "/api/resource/Quotation", 0, times=1, commit=True)
    r, mid = go(env, "A4")
    op = r.plan(mid)["steps"][0]["op_id"]
    env.stack.fail("erpnext", "GET", "/api/resource/Quotation", 502, times=3)
    with pytest.raises(ReconciliationError, match="cannot decide"):
        claim_done(r, mid, REQ, result=None)
    nothing_recorded_as_done(r, mid, op)


@pytest.mark.parametrize("reason", ["", "x", "short"])
def test_a_claim_needs_a_stated_reason_and_an_authenticated_principal(tmp_path, reason):
    env = Env(tmp_path)
    env.stack.fail("erpnext", "POST", "/api/resource/Quotation", 0, times=1, commit=True)
    r, mid = go(env, "A5")
    op = r.plan(mid)["steps"][0]["op_id"]
    with pytest.raises(ReconciliationError):
        claim_done(r, mid, REQ, result=None, reason=reason)
    with pytest.raises(ReconciliationError):
        claim_done(r, mid, None, result=None)
    nothing_recorded_as_done(r, mid, op)


def test_a_provider_enforced_step_is_held_to_the_same_read_back(tmp_path):
    from test_aosdd_redispatch_guard import S3_CAP
    env = Env(tmp_path)
    r, mid = parked(env, "A6", app="twenty", sub="/rest/tasks")                       # killed before Twenty's request: no task exists
    with pytest.raises(ReconciliationError, match="nothing to adopt"):
        claim_done(r, mid, REQ, cap=S3_CAP, result={"native_id": "fake-task"})
    assert state(r, mid) is MissionState.WAITING_RECONCILIATION and not env.stack.tasks


# ---- accepted: the application's record, verified ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("who", ["requester", "approver", "admin"])
def test_a_verified_record_is_adopted_through_the_runtime_directly_and_the_applications_record_is_what_is_kept(tmp_path, who):
    env = Env(tmp_path)
    env.stack.fail("erpnext", "POST", "/api/resource/Quotation", 0, times=1, commit=True)
    r, mid = go(env, "A7")
    st = r.plan(mid)["steps"][0]
    real = next(q["name"] for q in env.stack.quotations.values() if q.get("title") == st["derived"]["title"])
    claim_done(r, mid, {"requester": REQ, "approver": APP, "admin": ADM}[who], result={"native_id": real, "junk": "typed by a person"})
    assert state(r, mid) is MissionState.SUCCEEDED and env.stack.count("erpnext", "POST") == 1
    row = r.svc.oplog.get(st["op_id"])
    assert row["state"] == "applied" and row["native_id"] == real                                  # the adoption is in the operation ledger too
    ev = r.svc.store.for_mission(mid)
    assert any(e.type == "AosddStepVerified" and e.payload.get("during") == "reconcile" and e.payload["status"] == "VERIFIED" for e in ev)
    assert any(e.type == "AosddOpApplied" and e.payload.get("adopted") and e.payload["native_id"] == real for e in ev)
    done = [e.payload["result"] for e in ev if e.type == "NodeSucceeded" and e.payload.get("capability") == S1_CAP][0]
    assert done.get("native_id") == real and "junk" not in done                         # the caller's result was not what was kept
    rec = reconstruct(env.events_path, mid)
    assert rec["steps"][0]["adopted"] and rec["steps"][0]["classification"] != UNCERTAIN and not rec["steps"][0].get("asserted_without_read_back")


def test_runner_reconcile_goes_through_the_same_path(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.stack.fail("erpnext", "POST", "/api/resource/Quotation", 0, times=1, commit=True)
    r, mid = go(env, "A8")
    calls = []
    real = AosddMissionRuntime._verified_adoption

    def spy(self, *a, **k):
        calls.append(1)
        return real(self, *a, **k)
    monkeypatch.setattr(AosddMissionRuntime, "_verified_adoption", spy)
    r.reconcile(mid, "S1", principal=REQ)
    assert calls and state(r, mid) is MissionState.SUCCEEDED


def test_runner_reconcile_reports_a_refused_adoption_as_a_runner_error(tmp_path):
    from agentic_os.integrations.aosdd.runtime import RunnerError
    env = Env(tmp_path)
    r, mid = parked(env, "A10")
    st = r.plan(mid)["steps"][0]
    env.stack.quotations["SAL-QTN-LATE"] = {"name": "SAL-QTN-LATE", "title": st["derived"]["title"], "docstatus": 0, "party_name": "somebody else", "grand_total": 1.0}
    with pytest.raises(RunnerError, match="did not verify"):
        r.reconcile(mid, "S1", principal=REQ)
    nothing_recorded_as_done(r, mid, st["op_id"])


def test_the_supervisors_positive_evidence_gets_the_same_read_back_but_needs_no_role(tmp_path):
    from agentic_os.mission.principal import SYSTEM_PROBE_METHOD
    from tests._principals import authn
    probe = authn("system:reconciliation:aosdd-erpnext", tenant="*", roles=("system",), method=SYSTEM_PROBE_METHOD)
    env = Env(tmp_path)
    r, mid = parked(env, "A11")                                                         # nothing in the application: a probe's claim is refused as well
    with pytest.raises(ReconciliationError, match="nothing to adopt"):
        claim_done(r, mid, probe)
    env2 = Env(tmp_path / "second")
    (tmp_path / "second").mkdir()
    env2.stack.fail("erpnext", "POST", "/api/resource/Quotation", 0, times=1, commit=True)
    r2, mid2 = go(env2, "A12")
    claim_done(r2, mid2, probe, result=None)                                            # the record exists and verifies: adopted
    assert state(r2, mid2) is MissionState.SUCCEEDED


# ---- the receipt: no step can be "done" on a person's word ---------------------------------------------------------------------------------------------
def test_the_receipt_marks_a_step_completed_without_a_verified_adoption(tmp_path):
    """Not reachable through the API (above). A log written some other way (or a future code path) that completes the step on assertion is not read as clean."""
    env = Env(tmp_path)
    r, mid = parked(env, "A9")
    st = r.plan(mid)["steps"][0]
    node = node_of(r, mid, S1_CAP)
    r.svc.store.append("ReconciliationResolved", mid, {"node_id": node.id, "lease_id": "x", "executed": True, "actor": "someone", "reason": "typed by a person", "evidence": {}})
    r.svc.store.append("NodeSucceeded", mid, {"node_id": node.id, "capability": S1_CAP, "result": dict(ASSERTED, reconciled=True), "reconciled": True})
    rec = reconstruct(env.events_path, mid)
    s1 = rec["steps"][0]
    assert s1["asserted_without_read_back"] is True and s1["classification"] == UNCERTAIN and rec["mission_classification"] == UNCERTAIN
    assert any("ASSERTION" in x["what"] for x in s1["residual"])
