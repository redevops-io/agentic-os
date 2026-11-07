"""AOS-DD retry policy (v3): an UNKNOWN outcome of a step whose provider does not enforce idempotency is UNCERTAIN and is never re-sent automatically."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _principals import authn  # noqa: E402
from aosdd_fakes import APPROVAL_SECRET, Crash, FakeStack  # noqa: E402
from test_aosdd_runtime import ADM, APP, REF, REQ, TENANT, Env, go, state  # noqa: E402

from agentic_os.integrations.aosdd import approval as approvals
from agentic_os.integrations.aosdd.client import AosddOperatorClient, OperationUnresolved
from agentic_os.integrations.aosdd.context import IDEMPOTENCY, RETRY_POLICY, auto_retry_allowed
from agentic_os.integrations.aosdd.reconstruct import COMPENSATED, REVERSIBLE, UNCERTAIN, reconstruct
from agentic_os.integrations.aosdd.runtime import RunnerError
from agentic_os.mission.reconcile import ReconciliationError
from agentic_os.mission.types import MissionState

S1_CAP, S3_CAP, S4_CAP = "aosdd.erpnext.quotation_draft", "aosdd.twenty.task_create", "aosdd.chatwoot.private_note"


def uncertain_events(r, mid, step=None):
    return [e.payload for e in r.svc.store.for_mission(mid) if e.type == "AosddStepUncertain" and (step is None or e.payload["step"] == step)]


def crashed(env, case, app, method, sub, after):
    env.stack.crash(app, method, sub, after_effect=after)
    r = env.runner()
    mid = r.submit(case, REF, principal=REQ)
    with pytest.raises(Crash):
        r.approve_plan(mid, r.plan(mid)["plan_sha256"], principal=APP)
    r2 = env.runner()
    r2.resume(mid, principal=REQ, reason="the process was killed")
    return r2, mid


# ---- the table ---------------------------------------------------------------------------------------------------------------------
def test_the_retry_policy_is_derived_from_the_idempotency_class_per_step(tmp_path):
    assert IDEMPOTENCY == {"twenty": "provider-enforced", "lago": "provider-enforced", "erpnext": "marker-reconciled", "chatwoot": "marker-reconciled"}
    assert RETRY_POLICY == {"provider-enforced": "auto-same-op-id", "marker-reconciled": "operator-only", "none": "operator-only"}
    assert auto_retry_allowed("provider-enforced") and not auto_retry_allowed("marker-reconciled") and not auto_retry_allowed("none") and not auto_retry_allowed("anything-else")
    r, mid = go(Env(tmp_path), "TBL")
    assert [(s["step"], s["idempotency"], s["retry_policy"]) for s in r.plan(mid)["steps"]] == [
        ("S1", "marker-reconciled", "operator-only"), ("S2", "provider-enforced", "auto-same-op-id"), ("S3", "provider-enforced", "auto-same-op-id"), ("S4", "marker-reconciled", "operator-only")]
    rec = reconstruct(r.svc.store._path, mid)
    assert [(s["idempotency"], s["retry_policy"]) for s in rec["steps"]] == [("marker-reconciled", "operator-only"), ("provider-enforced", "auto-same-op-id"),
                                                                                ("provider-enforced", "auto-same-op-id"), ("marker-reconciled", "operator-only")]


# ---- ERPNext: crash after dispatch, before the request ---------------------------------------------------------------------------------
def test_erpnext_crash_before_the_request_parks_uncertain_and_only_an_approver_can_redispatch(tmp_path):
    env = Env(tmp_path)
    r, mid = crashed(env, "E1", "erpnext", "POST", "/api/resource/Quotation", after=False)
    assert state(r, mid) is MissionState.WAITING_RECONCILIATION and env.stack.count("erpnext", "POST") == 1       # the one POST that crashed
    assert env.stack.quotations.keys() == {"SAL-QTN-2026-00001"}                                                   # ... and created nothing
    u = uncertain_events(r, mid, "S1")
    assert u and u[0]["automatic_retry"] is False and u[0]["retry_policy"] == "operator-only"
    n = env.stack.count("erpnext", "POST")
    # no redispatch before an absence was observed
    with pytest.raises(RunnerError, match="no reconcile has observed"):
        r.redispatch(mid, "S1", "owner decided to retry the step", principal=APP)
    # reconcile: the lookup is complete and finds nothing -> absence is RECORDED, nothing is re-sent, the step stays uncertain
    r.reconcile(mid, "S1", principal=REQ)
    assert env.stack.count("erpnext", "POST") == n and state(r, mid) is MissionState.WAITING_RECONCILIATION
    assert [e for e in r.svc.store.for_mission(mid) if e.type == "AosddAbsenceObserved"] and r.svc.oplog.get(r.plan(mid)["steps"][0]["op_id"])["state"] == "inflight"
    # a requester may not redispatch; an approver may not either until the earlier lease + one HTTP timeout have passed
    with pytest.raises(RunnerError, match="may not redispatch"):
        r.redispatch(mid, "S1", "requester tries to retry", principal=REQ)
    with pytest.raises(RunnerError, match="too early"):
        r.redispatch(mid, "S1", "owner decided to retry the step", principal=APP)
    with pytest.raises(RunnerError, match="stated reason"):
        r.redispatch(mid, "S1", "x", principal=APP)
    assert env.stack.count("erpnext", "POST") == n
    env.shift = 200                                                                                      # lease 120 s + HTTP timeout 30 s have passed
    r.redispatch(mid, "S1", "owner decided to retry the step", principal=APP)
    assert state(r, mid) is MissionState.SUCCEEDED and len([q for q in env.stack.quotations.values() if q["docstatus"] == 0]) == 1
    auth = [e.payload for e in r.svc.store.for_mission(mid) if e.type == "AosddRedispatchAuthorized"][0]
    assert auth["principal"]["id"] == APP.id and auth["reason"] == "owner decided to retry the step" and auth["fresh_lookup_complete"] and "residual_risk" in auth
    rec = reconstruct(env.events_path, mid)
    assert rec["steps"][0]["uncertain"]["redispatch"][0]["principal"] == APP.id and rec["steps"][0]["classification"] == REVERSIBLE


def test_a_redispatch_is_refused_when_a_record_carrying_the_marker_exists_now(tmp_path):
    env = Env(tmp_path)
    r, mid = crashed(env, "E2", "erpnext", "POST", "/api/resource/Quotation", after=False)
    r.reconcile(mid, "S1", principal=REQ)
    env.shift = 200
    st = r.plan(mid)["steps"][0]
    env.stack.quotations["SAL-QTN-LATE"] = {"name": "SAL-QTN-LATE", "title": st["derived"]["title"], "docstatus": 0, "party_name": "x", "grand_total": 1.0}      # the earlier request landed after all
    n = env.stack.count("erpnext", "POST")
    with pytest.raises(RunnerError, match="exists now"):
        r.redispatch(mid, "S1", "owner decided to retry the step", principal=APP)
    assert env.stack.count("erpnext", "POST") == n


def test_a_redispatch_needs_a_complete_lookup(tmp_path):
    env = Env(tmp_path)
    r, mid = crashed(env, "E3", "erpnext", "POST", "/api/resource/Quotation", after=False)
    r.reconcile(mid, "S1", principal=REQ)
    env.shift = 200
    env.stack.fail("erpnext", "GET", "/api/resource/Quotation", 502, times=2)
    with pytest.raises(RunnerError, match="cannot decide"):
        r.redispatch(mid, "S1", "owner decided to retry the step", principal=APP)
    assert env.stack.count("erpnext", "POST") == 1


# ---- ERPNext / Chatwoot: the request reached the app, the reply was lost ---------------------------------------------------------------------
def test_erpnext_lost_reply_parks_uncertain_and_is_resolved_only_by_a_verified_adoption(tmp_path):
    env = Env(tmp_path)
    env.stack.fail("erpnext", "POST", "/api/resource/Quotation", 0, times=1, commit=True)
    r, mid = go(env, "E4")
    assert state(r, mid) is MissionState.WAITING_RECONCILIATION and uncertain_events(r, mid, "S1")[0]["automatic_retry"] is False
    assert env.stack.count("erpnext", "POST") == 1
    r.reconcile(mid, "S1", principal=REQ)                                                    # path (a): the effect exists -> adopt (verified) and go on
    assert state(r, mid) is MissionState.SUCCEEDED and env.stack.count("erpnext", "POST") == 1
    rec = reconstruct(env.events_path, mid)
    assert rec["steps"][0]["adopted"] and rec["steps"][0]["uncertain"]["marked"] and rec["steps"][0]["idempotency"] == "marker-reconciled"


def test_chatwoot_crash_after_the_reply_parks_uncertain_where_twenty_would_resume(tmp_path):
    env = Env(tmp_path)
    r, mid = crashed(env, "C1", "chatwoot", "POST", "/messages", after=True)                  # the note exists; the process died before recording it
    assert state(r, mid) is MissionState.WAITING_RECONCILIATION and sum(1 for m in env.stack.messages if m["private"]) == 1
    assert uncertain_events(r, mid, "S4")[0]["automatic_retry"] is False
    r.reconcile(mid, "S4", principal=REQ)                                                    # found by marker, verified, adopted: no second note
    assert state(r, mid) is MissionState.SUCCEEDED and sum(1 for m in env.stack.messages if m["private"]) == 1


def test_chatwoot_crash_before_the_request_needs_the_approvers_redispatch(tmp_path):
    env = Env(tmp_path)
    r, mid = crashed(env, "C2", "chatwoot", "POST", "/messages", after=False)
    assert state(r, mid) is MissionState.WAITING_RECONCILIATION and sum(1 for m in env.stack.messages if m["private"]) == 0
    r.reconcile(mid, "S4", principal=REQ)
    assert state(r, mid) is MissionState.WAITING_RECONCILIATION and sum(1 for m in env.stack.messages if m["private"]) == 0       # absence recorded, nothing sent
    env.shift = 200
    r.redispatch(mid, "S4", "owner decided to retry the note", principal=ADM)
    assert state(r, mid) is MissionState.SUCCEEDED and sum(1 for m in env.stack.messages if m["private"]) == 1


# ---- provider-enforced steps stay automatically resumable (same op id) --------------------------------------------------------------------------
def test_twenty_crash_after_the_reply_is_resumed_by_reconcile_without_an_approver(tmp_path):
    env = Env(tmp_path)
    r, mid = crashed(env, "T1", "twenty", "POST", "/rest/tasks", after=True)
    u = uncertain_events(r, mid, "S3")
    assert u and u[0]["automatic_retry"] is True and u[0]["retry_policy"] == "auto-same-op-id"
    r.reconcile(mid, "S3", principal=REQ)                                                    # a requester is enough: the provider rejects a duplicate id
    assert state(r, mid) is MissionState.SUCCEEDED and len(env.stack.tasks) == 1 and len(env.stack.task_targets) == 1


def test_a_provider_enforced_step_is_retried_automatically_under_the_same_op_id_and_adopts_what_exists(tmp_path):
    env = Env(tmp_path)
    env.stack.fail("lago", "PUT", "/api/v1/invoices", 0, times=1, commit=True)               # the entry was written, the reply lost
    r, mid = go(env, "L1")
    assert state(r, mid) is MissionState.WAITING_RECONCILIATION and env.stack.count("lago", "PUT") == 1
    plan = r.plan(mid)
    st = plan["steps"][1]
    q = [e.payload for e in r.svc.store.for_mission(mid) if e.type == "AosddOpApplied"][0]["native_id"]
    res = AosddOperatorClient(env.runner().svc)._write(mid, st["capability"], {"plan": {"plan": plan}, "quotation": q}, {"mission_id": mid, "tenant": TENANT, "envelope_digest": ""})
    assert res["adopted"] is True and res["created"] is False and env.stack.count("lago", "PUT") == 1                # no second PUT: the lookup found the entry
    from agentic_os.mission.store import EventStore
    assert [e for e in EventStore(env.events_path, strict=False).for_mission(mid) if e.type == "AosddOpRetried"]


def test_an_uncertain_non_provider_enforced_step_is_never_taken_over_by_the_client(tmp_path):
    env = Env(tmp_path)
    env.stack.fail("erpnext", "POST", "/api/resource/Quotation", 0, times=1, commit=True)
    r, mid = go(env, "E5")
    plan = r.plan(mid)
    n = env.stack.count("erpnext", "POST")
    with pytest.raises(OperationUnresolved):
        AosddOperatorClient(env.runner().svc)._write(mid, plan["steps"][0]["capability"], {"plan": {"plan": plan}}, {"mission_id": mid, "tenant": TENANT, "envelope_digest": ""})
    assert env.stack.count("erpnext", "POST") == n and not [e for e in r.svc.store.for_mission(mid) if e.type == "AosddOpRetried"]


# ---- nothing in the base can re-send an uncertain step ---------------------------------------------------------------------------------------------
def test_the_base_reconcile_path_cannot_redispatch_an_uncertain_non_provider_enforced_step(tmp_path):
    """The reconciliation supervisor calls reconcile_node(executed=False, resume=True) from a probe. For ERPNext/Chatwoot that is refused by the BASE
    runtime (the capability declares marker-reconciled idempotency): AutomaticRetryRefused, nothing sent, the reservation untouched."""
    env = Env(tmp_path)
    r, mid = crashed(env, "E6", "erpnext", "POST", "/api/resource/Quotation", after=False)
    node = next(n for n in r.rt._plans[mid].graph.nodes if n.capability == S1_CAP)
    n = env.stack.count("erpnext", "POST")
    system = authn("system:probe", tenant="*", roles=("admin",), method="system-probe")
    with pytest.raises(ReconciliationError, match="does not declare provider-enforced idempotency"):
        r.rt.reconcile_node(mid, node.id, executed=False, evidence={"kind": "provider_record", "reference": "probe: nothing found"}, reason="the probe says it did not happen", principal=system)
    assert env.stack.count("erpnext", "POST") == n and state(r, mid) is MissionState.WAITING_RECONCILIATION
    refused = [e for e in r.svc.store.for_mission(mid) if e.type == "AutomaticRetryRefused"]
    assert len(refused) == 1 and refused[0].payload["capability"] == S1_CAP and refused[0].payload["idempotency"] == "marker-reconciled" and refused[0].payload["uncertain"] is True


def _fork_with_unknown_outcome(env, case, bypass_admission):
    """A base fork branch with max_attempts=3 whose client call ends UNKNOWN (not_applied False). Returns (calls, runner, mission id)."""
    from agentic_os.integrations.aosdd.http import AppError
    from agentic_os.mission.fork_join import BranchSpec, ForkSpec, JoinSpec
    r = env.runner()
    mid = r.submit(case, REF, principal=REQ)
    plan = r.plan(mid)
    r.svc.emit("AosddPlanApproved", mid, approvals.mint(APPROVAL_SECRET, mid, plan, APP.id))
    r.issue_write_grant(mid, plan, APP)
    calls = []
    real = r.rt.executor.client.invoke

    def counting(operator, capability, inputs, idempotency_key, *, secrets=None):
        if capability == S1_CAP:
            calls.append(idempotency_key)
            raise AppError("erpnext", "unavailable", 0, "timeout after the request was sent")        # not_applied False: the effect may exist
        return real(operator, capability, inputs, idempotency_key, secrets=secrets)
    r.rt.executor.client.invoke = counting
    if bypass_admission:
        r.rt._pre_exec_ok = lambda *a, **k: True        # isolate the BASE retry behaviour from AOS-DD's own admission check
    st = plan["steps"][0]
    spec = ForkSpec("f1", [BranchSpec("b0", S1_CAP, inputs={"plan": {"plan": plan}, "party": st["target"]["party"], "step": "S1", "op_id": st["op_id"]}, max_attempts=3)], JoinSpec("all"))
    res = r.rt.forks.run_fork(mid, spec)
    return calls, res, r, mid


def test_fork_max_attempts_cannot_cause_a_second_dispatch_of_an_uncertain_step(tmp_path):
    """Base retry setting: a fork branch with max_attempts=3. An unknown outcome parks the branch; the client is called exactly once."""
    env = Env(tmp_path)
    calls, res, r, mid = _fork_with_unknown_outcome(env, "FORK1", bypass_admission=True)
    assert len(calls) == 1, calls
    assert env.stack.writes() == []
    types_ = [e.type for e in r.svc.store.for_mission(mid)]
    assert "BranchFailed" not in types_ or all(e.payload.get("will_retry") is False for e in r.svc.store.for_mission(mid) if e.type == "BranchFailed")


def test_a_fork_branch_for_an_aosdd_write_is_refused_before_any_dispatch(tmp_path):
    """AOS-DD's own admission check does not accept a fork branch's inputs for a write: fail closed, zero dispatches, whatever max_attempts says."""
    env = Env(tmp_path)
    calls, res, r, mid = _fork_with_unknown_outcome(env, "FORK2", bypass_admission=False)
    assert calls == [] and env.stack.writes() == [] and res.status == "failed"


# ---- accept as is ---------------------------------------------------------------------------------------------------------------------------------
def test_an_approver_can_accept_an_uncertain_outcome_as_is_which_cancels_the_mission_and_lists_the_residual(tmp_path):
    env = Env(tmp_path)
    env.stack.fail("erpnext", "POST", "/api/resource/Quotation", 0, times=1, commit=True)
    r, mid = go(env, "ACC")
    with pytest.raises(RunnerError, match="may not accept_uncertain"):
        r.accept_uncertain(mid, "S1", "requester tries to close it", principal=REQ)
    with pytest.raises(RunnerError, match="stated reason"):
        r.accept_uncertain(mid, "S1", "x", principal=APP)
    n = env.stack.count("erpnext", "POST")
    r.accept_uncertain(mid, "S1", "the owner accepts that the quotation may exist", principal=APP)
    assert state(r, mid) is MissionState.CANCELLED and env.stack.count("erpnext", "POST") == n
    st = r.plan(mid)["steps"][0]
    assert r.svc.oplog.get(st["op_id"])["state"] == "accepted-uncertain" and r.svc.oplog.claim(st["op_id"], "x").status == "accepted"
    rec = reconstruct(env.events_path, mid)
    s1 = rec["steps"][0]
    assert s1["classification"] == UNCERTAIN and s1["uncertain"]["accepted_as_is"]["principal"] == APP.id
    assert any("accepted as uncertain" in x["what"] for x in s1["residual"])
    with pytest.raises(RunnerError):
        r.redispatch(mid, "S1", "owner decided to retry the step", principal=APP)             # a closed uncertainty is not re-opened


def test_each_write_capability_declares_what_its_provider_does_about_a_repeat():
    """The base runtime re-dispatches on its own only for a capability that declares provider-enforced idempotency; AOS-DD declares it per application."""
    from agentic_os.integrations.aosdd.capabilities import WRITE_CAPS, specs
    declared = {s.name.rsplit(".", 2)[-2]: s.idempotency for s in specs() if s.name in WRITE_CAPS}
    assert declared == {"erpnext": "marker-reconciled", "lago": "provider-enforced", "twenty": "provider-enforced", "chatwoot": "marker-reconciled"}
    assert all(s.idempotency == "" for s in specs() if s.name not in WRITE_CAPS)
