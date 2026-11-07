"""AOS-DD hardening on top of the base rule (candidate/0.6.0-rc1): a PERSON's re-send of an uncertain ERPNext/Chatwoot step needs more than the base asks.

The base lets any authenticated person call ``reconcile_node(executed=False)`` and refuses only the automatic (supervisor) path. AOS-DD missions
additionally require, for a step whose provider does not enforce idempotency: the approver role, a stated reason, an earlier reconcile that RECORDED
the absence of the record, quiescence (the earlier lease expired at least one HTTP timeout ago) and a FRESH complete lookup that still finds nothing.
These conditions are enforced where the re-send happens (the runtime's ``reconcile_node``), so calling the runtime directly cannot skip them;
``Runner.redispatch`` uses the same guard."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from aosdd_fakes import Crash  # noqa: E402
from test_aosdd_runtime import ADM, APP, REF, REQ, Env, go, state  # noqa: E402

from agentic_os.integrations.aosdd.runtime import AosddMissionRuntime
from agentic_os.mission.reconcile import ReconciliationError
from agentic_os.mission.types import MissionState

S1_CAP, S3_CAP, S4_CAP = "aosdd.erpnext.quotation_draft", "aosdd.twenty.task_create", "aosdd.chatwoot.private_note"
EV = {"kind": "provider_record", "reference": "caller-supplied: nothing found"}
WHY = "owner decided to retry the step"


def parked(env, case, app="erpnext", method="POST", sub="/api/resource/Quotation"):
    """A mission whose write step was dispatched, the process killed BEFORE the request: UNCERTAIN, nothing in the application."""
    env.stack.crash(app, method, sub, after_effect=False)
    r = env.runner()
    mid = r.submit(case, REF, principal=REQ)
    with pytest.raises(Crash):
        r.approve_plan(mid, r.plan(mid)["plan_sha256"], principal=APP)
    r2 = env.runner()
    r2.resume(mid, principal=REQ, reason="the process was killed")
    assert state(r2, mid) is MissionState.WAITING_RECONCILIATION
    return r2, mid


def node_of(r, mid, cap):
    return next(n for n in r.rt._plans[mid].graph.nodes if n.capability == cap)


def direct(r, mid, cap, principal, reason=WHY, executed=False, evidence=EV, **kw):
    """The bypass the guard closes: the runtime object, called directly (no Runner)."""
    return r.rt.reconcile_node(mid, node_of(r, mid, cap).id, executed=executed, evidence=evidence, reason=reason, principal=principal, **kw)


def observed(env, r, mid, step="S1"):
    """A requester reconciles once (complete lookup, nothing found -> absence RECORDED, nothing sent), then the quiescence period passes."""
    r.reconcile(mid, step, principal=REQ)
    env.shift = 200


def posts(env, app):
    return env.stack.count(app, "POST")


def refused(r, mid):
    return [e.payload for e in r.svc.store.for_mission(mid) if e.type == "AosddRedispatchRefused"]


def assert_nothing_sent(env, r, mid, app, n, op):
    assert posts(env, app) == n
    assert state(r, mid) is MissionState.WAITING_RECONCILIATION
    assert r.svc.oplog.get(op)["state"] == "inflight"                       # the operation was not released for another claim
    assert not [e for e in r.svc.store.for_mission(mid) if e.type == "AosddRedispatchAuthorized"]


# ---- a direct call is held to the same conditions ----------------------------------------------------------------------------------------
def test_a_requester_calling_the_runtime_directly_cannot_re_send_even_when_every_other_condition_holds(tmp_path):
    env = Env(tmp_path)
    r, mid = parked(env, "G1")
    op = r.plan(mid)["steps"][0]["op_id"]
    observed(env, r, mid)
    n = posts(env, "erpnext")
    with pytest.raises(ReconciliationError, match="may not redispatch"):
        direct(r, mid, S1_CAP, REQ)
    assert_nothing_sent(env, r, mid, "erpnext", n, op)
    assert refused(r, mid) and refused(r, mid)[-1]["principal"]["id"] == REQ.id


def test_an_unauthenticated_caller_cannot_re_send(tmp_path):
    env = Env(tmp_path)
    r, mid = parked(env, "G2")
    op = r.plan(mid)["steps"][0]["op_id"]
    observed(env, r, mid)
    n = posts(env, "erpnext")
    with pytest.raises(ReconciliationError):
        direct(r, mid, S1_CAP, None, actor="someone typed this")           # the base would accept an anonymous actor only under a test policy
    assert_nothing_sent(env, r, mid, "erpnext", n, op)


def test_an_approver_without_a_recorded_absence_cannot_re_send(tmp_path):
    env = Env(tmp_path)
    r, mid = parked(env, "G3")
    op = r.plan(mid)["steps"][0]["op_id"]
    env.shift = 200
    n = posts(env, "erpnext")
    with pytest.raises(ReconciliationError, match="no reconcile has observed"):
        direct(r, mid, S1_CAP, APP)
    assert_nothing_sent(env, r, mid, "erpnext", n, op)


def test_an_approver_before_the_quiescence_period_cannot_re_send(tmp_path):
    env = Env(tmp_path)
    r, mid = parked(env, "G4")
    op = r.plan(mid)["steps"][0]["op_id"]
    r.reconcile(mid, "S1", principal=REQ)                                      # absence recorded, but the lease has not expired + one HTTP timeout
    n = posts(env, "erpnext")
    with pytest.raises(ReconciliationError, match="too early"):
        direct(r, mid, S1_CAP, APP)
    assert_nothing_sent(env, r, mid, "erpnext", n, op)


@pytest.mark.parametrize("reason", ["", "x", "      ", "short"])
def test_an_approver_without_a_stated_reason_cannot_re_send(tmp_path, reason):
    env = Env(tmp_path)
    r, mid = parked(env, "G5")
    op = r.plan(mid)["steps"][0]["op_id"]
    observed(env, r, mid)
    n = posts(env, "erpnext")
    with pytest.raises(ReconciliationError):
        direct(r, mid, S1_CAP, APP, reason=reason)
    assert_nothing_sent(env, r, mid, "erpnext", n, op)


def test_a_changed_lookup_stops_the_re_send_whatever_evidence_the_caller_supplies(tmp_path):
    """The caller's ``evidence`` is not trusted: the guard looks again. A record that appeared after the absence was observed refuses the re-send."""
    env = Env(tmp_path)
    r, mid = parked(env, "G6")
    st = r.plan(mid)["steps"][0]
    observed(env, r, mid)
    env.stack.quotations["SAL-QTN-LATE"] = {"name": "SAL-QTN-LATE", "title": st["derived"]["title"], "docstatus": 0, "party_name": "x", "grand_total": 1.0}
    n = posts(env, "erpnext")
    with pytest.raises(ReconciliationError, match="exists now"):
        direct(r, mid, S1_CAP, APP, evidence={"kind": "provider_record", "reference": "erpnext: no record carries the marker (fresh complete lookup)"})
    assert_nothing_sent(env, r, mid, "erpnext", n, st["op_id"])


def test_an_incomplete_fresh_lookup_stops_the_re_send(tmp_path):
    env = Env(tmp_path)
    r, mid = parked(env, "G7")
    op = r.plan(mid)["steps"][0]["op_id"]
    observed(env, r, mid)
    env.stack.fail("erpnext", "GET", "/api/resource/Quotation", 502, times=2)
    n = posts(env, "erpnext")
    with pytest.raises(ReconciliationError, match="cannot decide"):
        direct(r, mid, S1_CAP, APP)
    assert_nothing_sent(env, r, mid, "erpnext", n, op)


def test_chatwoot_is_held_to_the_same_conditions(tmp_path):
    env = Env(tmp_path)
    r, mid = parked(env, "G8", app="chatwoot", sub="/messages")
    op = r.plan(mid)["steps"][3]["op_id"]
    n = posts(env, "chatwoot")
    with pytest.raises(ReconciliationError, match="may not redispatch"):
        direct(r, mid, S4_CAP, REQ)
    with pytest.raises(ReconciliationError, match="no reconcile has observed"):
        direct(r, mid, S4_CAP, APP)
    assert posts(env, "chatwoot") == n and r.svc.oplog.get(op)["state"] == "inflight"
    observed(env, r, mid, "S4")
    direct(r, mid, S4_CAP, ADM)                                              # admin with everything in place: allowed
    assert state(r, mid) is MissionState.SUCCEEDED and sum(1 for m in env.stack.messages if m["private"]) == 1


# ---- the permitted path still works, and Runner.redispatch is the same guard ----------------------------------------------------------------------
def test_the_approver_with_every_condition_in_place_can_re_send_through_the_runtime_directly(tmp_path):
    env = Env(tmp_path)
    r, mid = parked(env, "G9")
    observed(env, r, mid)
    direct(r, mid, S1_CAP, APP)
    assert state(r, mid) is MissionState.SUCCEEDED and len([q for q in env.stack.quotations.values() if q["docstatus"] == 0]) == 1
    auth = [e.payload for e in r.svc.store.for_mission(mid) if e.type == "AosddRedispatchAuthorized"]
    assert len(auth) == 1 and auth[0]["principal"]["id"] == APP.id and auth[0]["reason"] == WHY and auth[0]["fresh_lookup_complete"] and "residual_risk" in auth[0]


def test_runner_redispatch_goes_through_the_same_guard(tmp_path, monkeypatch):
    env = Env(tmp_path)
    r, mid = parked(env, "G10")
    observed(env, r, mid)
    calls = []
    real = AosddMissionRuntime._redispatch_guard

    def spy(self, *a, **k):
        calls.append(1)
        return real(self, *a, **k)
    monkeypatch.setattr(AosddMissionRuntime, "_redispatch_guard", spy)
    r.redispatch(mid, "S1", WHY, principal=APP)
    assert calls and state(r, mid) is MissionState.SUCCEEDED
    # and a refusal by the guard is what Runner.redispatch reports
    monkeypatch.setattr(AosddMissionRuntime, "_redispatch_guard", lambda self, *a, **k: (_ for _ in ()).throw(ReconciliationError("guard says no")))
    (tmp_path / "second").mkdir()
    env2 = Env(tmp_path / "second")
    r2, mid2 = parked(env2, "G11")
    observed(env2, r2, mid2)
    n = posts(env2, "erpnext")
    from agentic_os.integrations.aosdd.runtime import RunnerError
    with pytest.raises(RunnerError, match="guard says no"):
        r2.redispatch(mid2, "S1", WHY, principal=APP)
    assert posts(env2, "erpnext") == n


# ---- unchanged: what the narrowed hook must NOT touch ------------------------------------------------------------------------------------------------
def test_a_provider_enforced_step_is_not_held_to_the_extra_conditions(tmp_path):
    """Twenty: a repeat of the same op id is safe (the provider rejects a duplicate id), so a requester's reconcile still resumes it."""
    env = Env(tmp_path)
    r, mid = parked(env, "G12", app="twenty", sub="/rest/tasks")
    r.reconcile(mid, "S3", principal=REQ)
    assert state(r, mid) is MissionState.SUCCEEDED and len(env.stack.tasks) == 1


def test_adopting_an_existing_record_is_not_blocked_by_the_re_send_guard(tmp_path):
    env = Env(tmp_path)
    env.stack.fail("erpnext", "POST", "/api/resource/Quotation", 0, times=1, commit=True)
    r, mid = go(env, "G13")
    r.reconcile(mid, "S1", principal=REQ)
    assert state(r, mid) is MissionState.SUCCEEDED and env.stack.count("erpnext", "POST") == 1
    assert len([q for q in env.stack.quotations.values() if q["docstatus"] == 0]) == 1
