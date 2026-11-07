"""Post-expiry in-flight completion race (v3 item 1): A's request is still in flight when its lease expires and B takes over; A's request then completes."""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(__file__))
from test_aosdd_runtime import ADM, APP, REF, REQ, Env, state  # noqa: E402

from agentic_os.integrations.aosdd.reconstruct import UNCERTAIN, reconstruct
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState


def test_a_request_completing_after_lease_expiry_is_stale_evidence_only_and_parks_the_mission(tmp_path):
    env = Env(tmp_path)
    entered, release = threading.Event(), threading.Event()
    env.stack.block("erpnext", "POST", "/api/resource/Quotation", entered, release)       # A's request is in flight (sent, not yet applied)
    ra = env.runner(owner="exec-A")
    mid = ra.submit("RACE", REF, principal=REQ)
    plan = ra.plan(mid)
    st = plan["steps"][0]
    out = {}

    def run_a():
        try:
            out["a"] = ra.approve_plan(mid, plan["plan_sha256"], principal=APP)
        except BaseException as e:  # noqa: BLE001
            out["a"] = e
    ta = threading.Thread(target=run_a)
    ta.start()
    assert entered.wait(15)
    env.shift = 200                                                                   # the 120 s lease is over; one HTTP timeout (30 s) more has passed
    # B (another process over the same files) takes over: it cannot dispatch (the reservation is dispatched), so it parks the step ...
    rb = env.runner(owner="exec-B")
    rb.resume(mid, principal=REQ, reason="A is silent past its lease")
    assert state(rb, mid) is MissionState.WAITING_RECONCILIATION
    # ... reconciles (complete lookup: nothing yet) -> absence recorded only; a requester cannot re-send; the approver does, on the record
    rb.reconcile(mid, "S1", principal=REQ)
    assert env.stack.count("erpnext", "POST") == 1                                       # still only A's, which is blocked
    rb.redispatch(mid, "S1", "A is silent past its lease; the owner approves a second attempt", principal=APP)
    assert state(rb, mid) is MissionState.SUCCEEDED                                      # B's (new generation) write went through and the mission finished
    b_row = rb.svc.oplog.get(st["op_id"])
    b_native, b_fence = b_row["native_id"], b_row["fence"]
    n_after_b = len(env.stack.writes())
    # NOW A's in-flight request completes, after expiry
    release.set()
    ta.join(30)
    assert not ta.is_alive()
    # A's result is stale-generation evidence only
    row = rb.svc.oplog.get(st["op_id"])
    assert row["state"] == "applied" and row["native_id"] == b_native and row["fence"] == b_fence                 # B's ledger state was not overwritten
    stale = rb.svc.oplog.stale_results(st["op_id"])
    assert len(stale) == 1 and stale[0]["native_id"] not in (None, "", b_native) and stale[0]["stale_fence"] < b_fence
    assert len(env.stack.writes()) == n_after_b                                       # A's request was already counted when it arrived; nobody sent anything after it
    markers = [q for q in env.stack.quotations.values() if q.get("title") == st["derived"]["title"]]
    assert len(markers) == 2                                                           # the possible duplicate effect is REAL here
    ev = EventStore(env.events_path, strict=False).for_mission(mid)
    types = [e.type for e in ev]
    late = types[types.index("AosddOpStale"):]
    assert "AosddStepUncertain" in late and "StaleResultRecorded" in late and "ReconciliationRequired" in late
    unc = [e.payload for e in ev if e.type == "AosddStepUncertain" and e.payload.get("stale_native_id")]
    assert unc and unc[-1]["stale_native_id"] == stale[0]["native_id"] and unc[-1]["automatic_retry"] is False and "possible duplicate effect" in unc[-1]["why"]
    # parked for the operator; no automatic retry, no automatic compensation, no failure
    assert not any(t in late for t in ("CompensationRequested", "CompensationExecuted", "NodeFailed", "AosddOpRetried", "NodeCompensated", "MissionOutcome"))
    assert [e.payload["state"] for e in ev if e.type == "MissionStateChanged"][-1] == "waiting_reconciliation"          # the ledger says: parked for the operator
    # the receipt: S1 is UNCERTAIN, both executors' native ids are listed as a residual
    rec = reconstruct(env.events_path, mid)
    s1 = rec["steps"][0]
    assert s1["classification"] == UNCERTAIN and rec["mission_classification"] == UNCERTAIN
    assert s1["uncertain"]["marked"] and s1["uncertain"]["stale_native_ids"] == [stale[0]["native_id"]] and s1["native_id"] == b_native
    dup = [x for x in s1["residual"] if "POSSIBLE DUPLICATE" in x["what"]]
    assert dup and dup[0]["native_ids"] == {"recorded_in_ledger": b_native, "stale_executor": [stale[0]["native_id"]]}
    # the operator's options are still the explicit ones: the unresolved duplicate makes an automatic adoption impossible (two markers)
    from agentic_os.integrations.aosdd.http import AppError
    import pytest
    with pytest.raises(AppError, match="refusing to pick one"):
        rb.svc.adapters["erpnext"].find(st)
    assert env.stack.count("erpnext", "DELETE") == 0                                  # nothing was deleted on anybody's behalf


def test_a_stale_answer_after_the_mission_failed_and_was_compensated_does_not_reopen_it(tmp_path):
    """R9 M3: B's second attempt gets past S1, S2 hits Lago's one-case-per-invoice guard, the mission FAILS and S1 is compensated; THEN A's old request
    reaches ERPNext. The log already says `failed`: the late answer is evidence only, the mission stays failed, nothing can resume it."""
    import pytest
    from agentic_os.integrations.aosdd.runtime import RunnerError
    from agentic_os.mission.reconcile import ReconciliationError
    env = Env(tmp_path)
    env.stack.lago_metadata.append({"lago_id": "foreign", "key": "aosdd_recovery", "value": "another-case"})      # another case already holds the invoice
    entered, release = threading.Event(), threading.Event()
    env.stack.block("erpnext", "POST", "/api/resource/Quotation", entered, release)
    ra = env.runner(owner="exec-A")
    mid = ra.submit("RACE-FAILED", REF, principal=REQ)
    plan = ra.plan(mid)
    st = plan["steps"][0]
    out = {}

    def run_a():
        try:
            out["a"] = ra.approve_plan(mid, plan["plan_sha256"], principal=APP)
        except BaseException as e:  # noqa: BLE001
            out["a"] = e
    ta = threading.Thread(target=run_a)
    ta.start()
    assert entered.wait(15)
    env.shift = 200
    rb = env.runner(owner="exec-B")
    rb.resume(mid, principal=REQ, reason="A is silent past its lease")
    rb.reconcile(mid, "S1", principal=REQ)
    rb.redispatch(mid, "S1", "A is silent past its lease; the owner approves a second attempt", principal=APP)
    assert state(rb, mid) is MissionState.FAILED                                          # B's S2 was refused by the domain guard; S1 was compensated
    assert not [q for q in env.stack.quotations.values() if q.get("title") == st["derived"]["title"]]          # B's quotation is gone
    n_after_b = len(env.stack.writes())
    release.set()
    ta.join(30)
    assert not ta.is_alive()
    markers = [q for q in env.stack.quotations.values() if q.get("title") == st["derived"]["title"]]
    assert len(markers) == 1                                                               # A's request landed: a quotation nobody recorded
    ev = EventStore(env.events_path, strict=False).for_mission(mid)
    types = [e.type for e in ev]
    late = types[types.index("AosddOpStale"):]
    assert "StaleResultRecorded" in late and "ReconciliationRequired" not in late and "NodeFailed" not in late
    assert not any(t in late for t in ("CompensationRequested", "AosddOpRetried", "MissionOutcome"))
    rec = [e.payload for e in ev if e.type == "StaleResultRecorded"][-1]
    assert rec["terminal"] is True and rec["mission_state"] == "failed"
    assert [e.payload["state"] for e in ev if e.type == "MissionStateChanged"][-1] == "failed"           # nothing written over the terminal state
    # the receipt: the mission is failed, S1 is UNCERTAIN, the extra quotation is a residual with both native ids
    r = reconstruct(env.events_path, mid)
    s1 = r["steps"][0]
    assert r["state"] == "failed" and s1["classification"] == UNCERTAIN and r["mission_classification"] == UNCERTAIN
    dup = [x for x in s1["residual"] if "POSSIBLE DUPLICATE" in x["what"]]
    assert dup and dup[0]["native_ids"]["stale_executor"]
    # nobody can continue it: no reconciliation is pending, a person's direct call is refused, a plain run does nothing
    node = next(n for n in rb.rt._plans[mid].graph.nodes if n.capability == st["capability"])
    for executed in (True, False):
        with pytest.raises(ReconciliationError):
            rb.rt.reconcile_node(mid, node.id, executed=executed, evidence={"kind": "provider_record", "reference": "x"}, reason="a person tries to continue it", principal=APP,
                                 result={"native_id": "SAL-QTN-X"} if executed else None)
    with pytest.raises(RunnerError):
        rb.reconcile(mid, "S1", principal=REQ)
    with pytest.raises(RunnerError):
        rb.redispatch(mid, "S1", "owner decided to retry the step", principal=APP)
    rb.rt.run(mid)
    assert len(env.stack.writes()) == n_after_b and state(rb, mid) is MissionState.FAILED          # nothing was sent after A's late request (it is already counted)
