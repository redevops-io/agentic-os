"""AOS-DD vertical slice through the REAL MissionRuntime against an in-process fake of the four apps (unit level).

The acceptance evidence comes from the live stack (tests/test_aosdd_live.py); these tests pin the behaviour: positive run,
authorization and approval scope enforced before any side effect, durable operation ids and reconciliation, concurrency,
compensation with residuals, read-only reconstruction, credential redaction."""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from aosdd_fakes import APPROVAL_SECRET, Crash, FakeStack, TOKENS, config_dict  # noqa: E402

from agentic_os.integrations.aosdd import approval as approvals
from agentic_os.integrations.aosdd.config import AosddConfig
from agentic_os.integrations.aosdd.context import plan_sha256
from agentic_os.integrations.aosdd.http import NetworkTouched
from agentic_os.integrations.aosdd.oplog import OpLog
from agentic_os.integrations.aosdd.reconstruct import COMPENSATED, REVERSIBLE, UNCERTAIN, reconstruct
from agentic_os.integrations.aosdd.runtime import Runner, RunnerError
from agentic_os.integrations.aosdd.services import RedactingEventStore, Services
from agentic_os.mission.authority import AuthorityGuard, SqliteEnvelopeStore, SqliteLedger
from agentic_os.mission.store import EventStore, verify_chain
from agentic_os.mission.types import MissionState

REF = "AOSDD-HFC-001"


class Env:
    """Files on disk + a fresh Services/Runner per call to ``runner()`` (a new process over the same durable state)."""

    def __init__(self, tmp_path, stack=None, cfg=None):
        self.dir = str(tmp_path)
        self.stack = stack or FakeStack()
        self.cfg = cfg or AosddConfig.from_dict(config_dict())
        self.shift = 0.0
        self.events_path = self.dir + "/events.jsonl"

    def now(self):
        return time.time() + self.shift

    def services(self, owner=None):
        guard = AuthorityGuard(SqliteEnvelopeStore(self.dir + "/auth.db"), SqliteLedger(self.dir + "/auth.db.usage"), clock=self.now)
        store = RedactingEventStore(self.events_path, self.cfg.redactor)
        return Services(self.cfg, store, OpLog(self.dir + "/ops.db"), guard, transport=self.stack, clock=self.now, owner=owner)

    def runner(self, owner=None, **kw):
        return Runner(self.services(owner), **kw)


@pytest.fixture
def env(tmp_path):
    os.environ.pop("AGENTIC_OS_APPROVAL_SECRET", None)
    return Env(tmp_path)


def go(env, case="CASE-1", approve=True):
    r = env.runner()
    mid = r.submit(case, REF)
    if approve:
        r.approve_plan(mid, "alice@example", r.plan(mid)["plan_sha256"])
    return r, mid


def types(r, mid):
    return [e.type for e in r.svc.store.for_mission(mid)]


def state(r, mid):
    return r.rt.repo.state(mid)


# ---- positive ---------------------------------------------------------------------------------------------------------------------
def test_positive_run_four_writes_nine_reads_verified_by_independent_readback(env):
    r, mid = go(env)
    assert state(r, mid) is MissionState.SUCCEEDED
    assert [(a, m) for a, m, p in env.stack.writes()] == [("erpnext", "POST"), ("lago", "PUT"), ("twenty", "POST"), ("twenty", "POST"), ("chatwoot", "POST")]
    rec = reconstruct(env.events_path, mid)
    assert len(rec["context_pack"]) == 9 and {c["app"] for c in rec["context_pack"]} == {"lago", "erpnext", "chatwoot", "twenty"}
    assert [s["classification"] for s in rec["steps"]] == [REVERSIBLE] * 4
    for s in rec["steps"]:
        assert s["verification"]["status"] == "VERIFIED" and s["verification"]["method"] == "independent_readback" and s["native_id"] and s["ui_url"]
        assert s["request_persisted_before_send"] is True
    assert rec["steps"][0]["native_id"].startswith("SAL-QTN-") and rec["steps"][2]["ui_url"].endswith("/object/task/" + rec["steps"][2]["native_ids"]["task"])
    assert "no per-message URL" in rec["steps"][3]["ui_url_note"] and rec["steps"][3]["ui_url"].endswith("/conversations/1")
    assert rec["ledger"]["mission_chain"]["ok"] and rec["ledger"]["store_chain"]["ok"] and rec["ledger"]["mission_chain"]["unchained"] == 0
    assert rec["approval"]["approver"] == "alice@example" and rec["approval"]["plan_sha256"] == rec["plan_sha256"]
    # every authority decision is in the ledger, one per node, with the envelope digest
    assert sum(1 for a in rec["authorizations"] if a["type"] == "ScopeAuthorized") == 6


def test_verification_reads_use_reader_credentials_and_separate_clients(env):
    r, mid = go(env)
    verify_start = max(i for i, c in enumerate(env.stack.calls) if c[1] != "GET")
    reads_after_last_write = list(zip(env.stack.calls, env.stack.roles))[verify_start + 1:]
    assert reads_after_last_write and all(role == "reader" for (app, m, p), role in reads_after_last_write if app in ("twenty", "erpnext"))
    svc = r.svc
    assert all(svc.readback.c[a] is not svc.writers[a] for a in svc.writers)
    assert all(h.read_only for h in svc.readback.c.values())


# ---- authorization and approval scope: nothing is sent unless every check passes ---------------------------------------------------------------
def test_a_plan_hash_that_is_not_the_plans_current_hash_approves_nothing(env):
    r = env.runner()
    mid = r.submit("C", REF)
    with pytest.raises(RunnerError, match="not the plan's current hash"):
        r.approve_plan(mid, "alice", "0" * 64)
    assert env.stack.writes() == [] and state(r, mid) is MissionState.WAITING_HUMAN and "AosddPlanApproved" not in types(r, mid)


def test_changed_plan_after_approval_fails_closed_with_zero_side_effects(env):
    r = env.runner()
    mid = r.submit("C", REF)
    plan = json.loads(json.dumps(r.plan(mid)))
    sha = plan["plan_sha256"]
    # an attacker (or a bug) appends a different plan observation after the human reviewed `sha`: S4 now targets another conversation
    plan["steps"][3]["target"]["conversation_id"] = 2
    plan["plan_sha256"] = plan_sha256(plan)
    r.svc.emit("ObservationWritten", mid, {"key": "plan", "value": {"status": "ok", "executed": True, "plan": plan, "plan_sha256": plan["plan_sha256"]}, "source": "evil", "confidence": 100.0, "source_type": "human", "source_ref": ""})
    # the approval is for the ORIGINAL hash; the gate still passes the runtime's own input check only if the digest matches, so approve directly
    tok = approvals.mint(APPROVAL_SECRET, mid, json.loads(json.dumps(r.plan(mid))), "alice")      # approval of the ledger's plan
    r.svc.emit("AosddPlanApproved", mid, tok)
    r.issue_write_grant(mid, r.plan(mid), "alice")
    m = r.rt.approve(mid, r.rt.repo.pending_human(mid)["node_id"], "approve", actor="alice")
    assert env.stack.writes() == []
    assert state(r, mid) in (MissionState.FAILED, MissionState.WAITING_HUMAN)
    assert not any(a == "erpnext" and m_ == "POST" for a, m_, p in env.stack.calls)


def test_approval_binds_step_set_and_targets_not_just_the_hash(env):
    r = env.runner()
    mid = r.submit("C", REF)
    plan = r.plan(mid)
    tok = approvals.mint(APPROVAL_SECRET, mid, plan, "alice")
    ok, _ = approvals.verify(APPROVAL_SECRET, tok, mid, plan)
    assert ok
    # same hash claimed, but a step was dropped / retargeted in the plan presented now
    p2 = json.loads(json.dumps(plan))
    p2["steps"].pop()
    assert approvals.verify(APPROVAL_SECRET, tok, mid, p2)[0] is False
    p3 = json.loads(json.dumps(plan))
    p3["steps"][1]["target"]["invoice_lago_id"] = "other"
    assert approvals.verify(APPROVAL_SECRET, tok, mid, p3)[0] is False
    p4 = json.loads(json.dumps(plan))
    p4["steps"][1]["target"]["invoice_lago_id"] = "other"
    p4["plan_sha256"] = plan_sha256(p4)
    assert "plan_sha256" in approvals.verify(APPROVAL_SECRET, tok, mid, p4)[1]


def test_replayed_and_expired_approvals_are_refused(env):
    r = env.runner()
    a = r.submit("A", REF)
    b = r.submit("B", REF)
    tok = approvals.mint(APPROVAL_SECRET, a, r.plan(a), "alice")
    assert approvals.verify(APPROVAL_SECRET, tok, b, r.plan(b))[0] is False                      # another mission's approval
    assert "different mission" in approvals.verify(APPROVAL_SECRET, tok, b, r.plan(a))[1]
    forged = {"claims": tok["claims"], "sig": "0" * 64}
    assert "signature" in approvals.verify(APPROVAL_SECRET, forged, a, r.plan(a))[1]
    assert "expired" in approvals.verify(APPROVAL_SECRET, approvals.mint(APPROVAL_SECRET, a, r.plan(a), "alice", ttl_s=1, now=time.time() - 100), a, r.plan(a))[1]
    # replay by copying the event into mission B's ledger: B's steps are refused, nothing is sent
    r.svc.emit("AosddPlanApproved", b, tok)
    r.issue_write_grant(b, r.plan(b), "alice")
    r.rt.approve(b, r.rt.repo.pending_human(b)["node_id"], "approve", actor="mallory")
    assert env.stack.writes() == [] and state(r, b) is MissionState.FAILED
    assert any(e.type == "NodeFailed" and "different mission" in e.payload["error"] for e in r.svc.store.for_mission(b))


def test_wrong_target_in_the_owners_grant_is_out_of_scope(env):
    r = env.runner()
    mid = r.submit("C", REF)
    plan = json.loads(json.dumps(r.plan(mid)))
    r.svc.emit("AosddPlanApproved", mid, approvals.mint(APPROVAL_SECRET, mid, plan, "alice"))
    other = json.loads(json.dumps(plan))
    other["steps"][1]["target"]["invoice_lago_id"] = "someone-elses-invoice"       # the grant covers a different invoice than the plan
    r.issue_write_grant(mid, other, "alice")
    r.rt.approve(mid, r.rt.repo.pending_human(mid)["node_id"], "approve", actor="alice")
    # S1 (party matches) ran; S2 is refused by the grant BEFORE any Lago request
    assert [a for a, m, p in env.stack.writes()] == ["erpnext"]
    assert state(r, mid) is MissionState.FAILED
    halt = [e for e in r.svc.store.for_mission(mid) if e.type == "AosddMissionHalted"][0].payload
    assert halt["rule"] == "authority_resource_out_of_scope" and halt["effects_remaining"] == ["aosdd.erpnext.quotation_draft"]


def test_expired_grant_stops_the_mission_before_the_first_write(env):
    r = env.runner()
    mid = r.submit("C", REF)
    plan = r.plan(mid)
    r.approve_plan(mid, "alice", plan["plan_sha256"], grant_ttl_s=-5) if False else None
    tok = approvals.mint(APPROVAL_SECRET, mid, plan, "alice")
    r.svc.emit("AosddPlanApproved", mid, tok)
    r.issue_write_grant(mid, plan, "alice", ttl_s=5)
    env.shift = 60                                                       # the grant is 5 s long; a minute passes
    r.rt.approve(mid, r.rt.repo.pending_human(mid)["node_id"], "approve", actor="alice")
    assert env.stack.writes() == [] and state(r, mid) is MissionState.FAILED
    assert [e for e in r.svc.store.for_mission(mid) if e.type == "AosddMissionHalted"][0].payload["rule"] == "authority_expired"


def test_revoking_the_grant_mid_mission_stops_the_remaining_steps_immediately(env):
    r = env.runner()
    mid = r.submit("C", REF)

    def revoke_after_s2(ev):
        if ev.type == "NodeSucceeded" and ev.payload.get("capability") == "aosdd.lago.invoice_metadata":
            r.revoke(mid, "owner@example", "customer disputed")
    r.svc.store.subscribe(revoke_after_s2)
    r.approve_plan(mid, "alice", r.plan(mid)["plan_sha256"])
    assert [a for a, m, p in env.stack.writes()] == ["erpnext", "lago"]            # S3 and S4 were never sent
    assert state(r, mid) is MissionState.FAILED
    rec = reconstruct(env.events_path, mid)
    assert [s["outcome"] for s in rec["steps"]] == ["applied", "applied", "not_attempted", "not_attempted"]
    assert rec["halts"][0]["rule"] == "authority_revoked" and rec["mission_classification"] == REVERSIBLE
    assert any(e["type"] == "AosddGrantRevoked" for e in rec["grants"])
    # nothing was undone automatically ...
    assert env.stack.count("erpnext", "DELETE") == 0
    # ... undoing is an explicit, attributed operator action, verified by read-back
    r.compensate(mid, "ops@example", "grant revoked by owner")
    rec = reconstruct(env.events_path, mid)
    assert [s["classification"] for s in rec["steps"][:2]] == [COMPENSATED, COMPENSATED]
    assert env.stack.quotations.keys() == {"SAL-QTN-2026-00001"} and env.stack.lago_metadata == []


def test_a_revoked_grant_before_approval_sends_nothing(env):
    r = env.runner()
    mid = r.submit("C", REF)
    r.revoke(mid, "owner", "no")
    r.approve_plan(mid, "alice", r.plan(mid)["plan_sha256"])
    assert env.stack.writes() == []


# ---- duplicates ----------------------------------------------------------------------------------------------------------------------------------
def test_same_case_submitted_twice_is_one_mission_and_one_set_of_records(env):
    r1, mid = go(env, "DUP")
    r2 = env.runner()
    mid2 = r2.submit("DUP", REF)
    assert mid2 == mid and len(r2.svc.store.mission_ids()) == 1
    assert "AosddDuplicateSubmit" in [e.type for e in r2.svc.store.for_mission(mid)]
    assert len(env.stack.writes()) == 5 and len(env.stack.quotations) == 2 and len(env.stack.tasks) == 1
    assert sum(1 for m in env.stack.messages if m["private"]) == 1


def test_retrying_a_completed_step_sends_nothing(env):
    r, mid = go(env)
    n = len(env.stack.writes())
    from agentic_os.integrations.aosdd.client import AosddOperatorClient
    plan = r.plan(mid)
    for st in plan["steps"]:
        quotation = {"native_id": [e for e in r.svc.store.for_mission(mid) if e.type == "AosddOpApplied"][0].payload["native_id"]}
        res = AosddOperatorClient(env.runner().svc).invoke("aosdd", st["capability"], {"_mission": mid, "plan": {"plan": plan}, "quotation": quotation, "_exec_auth": {
            "mission_id": mid, "tenant": env.cfg.tenant, "envelope_digest": ""}}, "k")
        assert res["replayed"] is True and res["created"] is False
    assert len(env.stack.writes()) == n


def test_a_lost_reply_is_adopted_by_marker_never_written_twice(env):
    """The write reached ERPNext; the reply was lost. ERPNext has no de-duplication: the retry must find its own record."""
    env.stack.fail("erpnext", "POST", "/api/resource/Quotation", 0, times=1, commit=True)
    r, mid = go(env)
    assert state(r, mid) is MissionState.WAITING_RECONCILIATION
    assert len([q for q in env.stack.quotations.values() if q["docstatus"] == 0]) == 1
    r.reconcile(mid, "S1", "ops@example")
    assert state(r, mid) is MissionState.SUCCEEDED
    assert len([q for q in env.stack.quotations.values() if q["docstatus"] == 0]) == 1                       # still exactly one
    rec = reconstruct(env.events_path, mid)
    assert rec["steps"][0]["adopted"] is True and rec["steps"][0]["classification"] == REVERSIBLE and rec["steps"][0]["verification"]["status"] == "VERIFIED"


def test_exactly_one_of_many_concurrent_executors_performs_the_write(env):
    r = env.runner()
    mid = r.submit("C", REF)
    plan = r.plan(mid)
    r.svc.emit("AosddPlanApproved", mid, approvals.mint(APPROVAL_SECRET, mid, plan, "alice"))
    from agentic_os.integrations.aosdd.client import AosddOperatorClient, OperationUnresolved
    results, barrier = [], threading.Barrier(8)
    st = plan["steps"][0]

    def attempt():
        c = AosddOperatorClient(env.runner().svc)
        barrier.wait()
        try:
            results.append(c._write(mid, st["capability"], {"plan": {"plan": plan}}, {"mission_id": mid, "tenant": env.cfg.tenant, "envelope_digest": ""}))
        except Exception as e:  # noqa: BLE001
            results.append(e)
    # authority is not the point here (covered above): let the recheck pass by granting S1
    r.issue_write_grant(mid, plan, "alice")
    ts = [threading.Thread(target=attempt) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert env.stack.count("erpnext", "POST") == 1
    assert sum(1 for x in results if isinstance(x, dict) and x["created"]) == 1


# ---- restart ---------------------------------------------------------------------------------------------------------------------------------------
def test_kill_after_the_request_was_applied_parks_on_restart_and_reconcile_completes_without_duplicating(env):
    env.stack.crash("twenty", "POST", "/rest/tasks", after_effect=True)           # S3's task is created, then the process dies (its company link never was)
    r = env.runner()
    mid = r.submit("C", REF)
    with pytest.raises(Crash):
        r.approve_plan(mid, "alice", r.plan(mid)["plan_sha256"])
    assert len(env.stack.tasks) == 1 and len(env.stack.writes()) == 3 and env.stack.task_targets == {}
    r2 = env.runner()                                                                # a new process over the same files
    r2.attach(mid)
    n = len(env.stack.writes())
    assert len(env.stack.writes()) == n and state(r2, mid) is MissionState.RUNNING   # attach alone sends nothing and runs nothing
    r2.resume(mid, "ops@example", "process was killed")                               # explicit; the runtime refuses to redispatch S3
    assert state(r2, mid) is MissionState.WAITING_RECONCILIATION and len(env.stack.writes()) == n
    r2.reconcile(mid, "S3", "ops@example")                                           # the task is found but half-applied: S3 is dispatched again and COMPLETES it
    assert state(r2, mid) is MissionState.SUCCEEDED
    assert len(env.stack.tasks) == 1 and len(env.stack.task_targets) == 1             # one task, one link: no duplicate
    assert env.stack.count("erpnext", "POST") == 1 and env.stack.count("lago", "PUT") == 1 and sum(1 for m in env.stack.messages if m["private"]) == 1


def test_kill_before_the_request_resumes_and_writes_once(env):
    env.stack.crash("twenty", "POST", "/rest/tasks", after_effect=False)
    r = env.runner()
    mid = r.submit("C", REF)
    with pytest.raises(Crash):
        r.approve_plan(mid, "alice", r.plan(mid)["plan_sha256"])
    assert len(env.stack.tasks) == 0
    r2 = env.runner()
    r2.resume(mid, "ops@example", "killed")
    assert state(r2, mid) is MissionState.WAITING_RECONCILIATION
    r2.reconcile(mid, "S3", "ops@example")                                           # the marker is absent: safe to dispatch again
    assert state(r2, mid) is MissionState.SUCCEEDED
    assert len(env.stack.tasks) == 1 and env.stack.count("erpnext", "POST") == 1 and env.stack.count("lago", "PUT") == 1
    assert sum(1 for m in env.stack.messages if m["private"]) == 1


def test_resume_is_never_automatic_and_needs_a_named_actor(env):
    env.stack.crash("lago", "PUT", "/api/v1/invoices", after_effect=False)
    r = env.runner()
    mid = r.submit("C", REF)
    with pytest.raises(Crash):
        r.approve_plan(mid, "alice", r.plan(mid)["plan_sha256"])
    n = len(env.stack.calls)
    r2 = env.runner()
    r2.attach(mid)
    assert len(env.stack.calls) == n
    with pytest.raises(RunnerError, match="name the actor"):
        r2.resume(mid, " ")


# ---- partial failure and compensation --------------------------------------------------------------------------------------------------------
def test_failure_at_s3_compensates_s2_then_s1_in_reverse_order_each_verified(env):
    env.stack.fail("twenty", "POST", "/rest/tasks", 403, times=5, body={"statusCode": 403, "messages": ["This API Key is revoked"]})
    r, mid = go(env)
    assert state(r, mid) is MissionState.FAILED
    rec = reconstruct(env.events_path, mid)
    assert [s["classification"] for s in rec["steps"]] == [COMPENSATED, COMPENSATED, "none", "none"] and rec["mission_classification"] == COMPENSATED
    assert rec["steps"][2]["outcome"] == "refused" and rec["steps"][2]["error"]["kind"] == "credential"
    assert env.stack.quotations.keys() == {"SAL-QTN-2026-00001"} and env.stack.lago_metadata == [] and env.stack.tasks == {}
    order = [e.payload["node_id"] for e in r.svc.store.for_mission(mid) if e.type == "CompensationExecuted"]
    assert [rec["steps"][1]["node_id"], rec["steps"][0]["node_id"]] == order                              # S2 first, then S1
    for s in rec["steps"][:2]:
        assert s["compensation"]["readback"]["status"] == "VERIFIED"
    out = r.rt._missions[mid].outcome
    assert out["rolled_back"] is False and out["compensation_clean_rollback"] is False and "residual" in out["compensation_note"]        # never a silent claim of a clean rollback
    r2 = env.runner()
    r2.attach(mid)
    assert r2.rt._missions[mid].outcome["rolled_back"] is False                                                    # nor after a restart


def test_failure_at_s4_compensates_s3_s2_s1_and_records_the_twenty_residual(env):
    env.stack.fail("chatwoot", "POST", "/messages", 401, times=5, body={"error": "Invalid Access Token"})
    r, mid = go(env)
    assert state(r, mid) is MissionState.FAILED
    rec = reconstruct(env.events_path, mid)
    assert [s["classification"] for s in rec["steps"]] == [COMPENSATED] * 3 + ["none"]
    assert len(env.stack.tasks) == 1 and next(iter(env.stack.tasks.values()))["deletedAt"]                       # the soft-deleted row REMAINS
    assert any("soft-deleted" in x["what"] for x in rec["steps"][2]["residual"])
    assert rec["residual_effects"] and {x["step"] for x in rec["residual_effects"]} >= {"S2", "S3"}


def test_a_task_that_cannot_be_linked_is_taken_back_inside_the_step(env):
    env.stack.fail("twenty", "POST", "/rest/taskTargets", 400, times=3, body={"messages": ["PERMISSION_DENIED"]})
    r, mid = go(env)
    assert next(iter(env.stack.tasks.values()))["deletedAt"]
    rec = reconstruct(env.events_path, mid)
    assert rec["steps"][2]["outcome"] == "refused" and rec["steps"][2]["classification"] == COMPENSATED
    assert rec["steps"][2]["residual"][0]["rolled_back"] is True


def test_lago_domain_guard_refuses_to_clobber_foreign_metadata_and_compensates_s1(env):
    env.stack.lago_metadata = [{"lago_id": "x", "key": "other_system", "value": "keep me"}]
    r, mid = go(env)
    assert state(r, mid) is MissionState.FAILED
    assert env.stack.count("lago", "PUT") == 0 and env.stack.lago_metadata[0]["key"] == "other_system"
    assert env.stack.quotations.keys() == {"SAL-QTN-2026-00001"}


def test_a_write_the_readback_does_not_confirm_is_compensated_and_never_committed(env):
    def corrupt(stack):
        q = [q for q in stack.quotations.values() if q.get("title", "").startswith("AOSDD-")][0]
        q["grand_total"] = 1.0
    env.stack.on_effect("erpnext", "POST", "/api/resource/Quotation", corrupt)
    r, mid = go(env)
    assert state(r, mid) is MissionState.FAILED
    assert env.stack.quotations.keys() == {"SAL-QTN-2026-00001"}                    # the unverified draft was deleted
    rec = reconstruct(env.events_path, mid)
    assert rec["steps"][0]["verification"]["status"] == "NOT_VERIFIED" and rec["steps"][0]["classification"] == COMPENSATED
    assert "NodeSucceeded" not in [e.type for e in r.svc.store.for_mission(mid) if e.payload.get("capability") == "aosdd.erpnext.quotation_draft"]
    assert env.stack.count("lago", "PUT") == 0


def test_an_unreadable_verifier_never_verifies_and_never_silently_rejects(env):
    env.stack.fail("erpnext", "GET", "/api/resource/Quotation/", 502, times=5)           # the independent read-back cannot read
    r, mid = go(env)
    assert state(r, mid) is MissionState.WAITING_HUMAN                                     # ESCALATE: a human decides
    rec = reconstruct(env.events_path, mid)
    assert rec["steps"][0]["verification"]["status"] == "UNREADABLE" and rec["steps"][0]["classification"] == UNCERTAIN


def test_rerun_after_compensation_needs_a_new_attempt_and_a_new_twenty_id(env):
    env.stack.fail("chatwoot", "POST", "/messages", 401, times=1, body={"error": "x"})
    r, mid = go(env, "RR")
    first_task = next(iter(env.stack.tasks))
    r2 = env.runner()
    mid2 = r2.submit("RR", REF, attempt=2)
    assert mid2 != mid
    r2.approve_plan(mid2, "alice", r2.plan(mid2)["plan_sha256"])
    assert state(r2, mid2) is MissionState.SUCCEEDED
    live = [t for t in env.stack.tasks.values() if not t["deletedAt"]]
    assert len(live) == 1 and live[0]["id"] != first_task


# ---- reconstruction ----------------------------------------------------------------------------------------------------------------------------------
def test_reconstruction_makes_no_connector_or_network_call_and_matches_the_live_receipt(env, monkeypatch):
    r, mid = go(env)
    calls_before = len(env.stack.calls)

    def boom(*a, **k):
        raise AssertionError("network touched during reconstruction")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    rec = reconstruct(env.events_path, mid)                                # path only: no runtime, no Services, no transport
    assert len(env.stack.calls) == calls_before and rec["state"] == "succeeded"
    assert [s["native_id"] for s in rec["steps"]][0].startswith("SAL-QTN")
    from agentic_os.integrations.aosdd.http import UrllibTransport, Request
    from agentic_os.integrations.aosdd.http import network_disabled
    with network_disabled(), pytest.raises(NetworkTouched):
        UrllibTransport().send(Request("GET", "http://127.0.0.1:1/", {}, None, 1))
    assert reconstruct(env.events_path, mid) == rec                       # deterministic


def test_reconstruction_detects_a_tampered_ledger(env, tmp_path):
    r, mid = go(env)
    lines = open(env.events_path).read().splitlines()
    d = json.loads(lines[30])
    d["payload"] = {**d["payload"], "tampered": True}
    lines[30] = json.dumps(d)
    p = tmp_path / "tampered.jsonl"
    p.write_text("\n".join(lines) + "\n")
    assert reconstruct(str(p), mid)["ledger"]["mission_chain"]["ok"] is False


# ---- credential redaction ----------------------------------------------------------------------------------------------------------------------------------
def _all_secrets():
    return ["cwTOKENwriter0001", "twTOKENwriter0002", "twTOKENreader0003", "erpKEYwriter", "erpSECRETwriter", "erpKEYreader", "erpSECRETreader", "lagoKEYsecret0004", APPROVAL_SECRET]


def test_no_credential_reaches_ledger_receipt_errors_or_logs(env, caplog, capsys):
    caplog.set_level("DEBUG")
    # an application that echoes the credentials it was sent, in an error body, into the ledger-bound error path
    env.stack.fail("twenty", "POST", "/rest/tasks", 500, times=1, body={"error": "boom twTOKENwriter0002 Authorization: Bearer twTOKENwriter0002 erpKEYwriter:erpSECRETwriter"})
    r, mid = go(env)
    rec = json.dumps(reconstruct(env.events_path, mid), default=str)
    ledger = open(env.events_path).read()
    ops = json.dumps([r.svc.oplog.get(s["op_id"]) for s in r.plan(mid)["steps"]], default=str)
    out = capsys.readouterr()
    sinks = {"ledger": ledger, "receipt": rec, "oplog": ops, "log": caplog.text, "stdout": out.out + out.err}
    for name, text in sinks.items():
        for sec in _all_secrets():
            assert sec not in text, (name, "leaked a credential")
    assert "[REDACTED]" in ledger                                                  # the echo was seen and scrubbed
    # exception text
    from agentic_os.integrations.aosdd.http import AppError
    try:
        r.svc.writers["lago"].check(env.stack.send.__self__ and r.svc.writers["lago"].get("/nope"))
    except AppError as e:
        assert all(s not in str(e) for s in _all_secrets())


def test_config_and_services_repr_never_contain_credentials(env):
    r = env.runner()
    blob = repr(env.cfg) + repr(r.svc.writers) + repr(r.svc.readback.c) + str(vars(r.svc.cfg))
    assert all(s not in blob for s in _all_secrets())


def test_an_application_that_is_down_before_the_write_is_a_definite_refusal_not_an_unknown(env):
    env.stack.fail("twenty", "GET", "/rest/tasks/", 502, times=9)                 # the lookup (a read) fails with a gateway error: nothing was written
    r, mid = go(env)
    assert state(r, mid) is MissionState.FAILED                                   # not parked for reconciliation: the write was never sent
    rec = reconstruct(env.events_path, mid)
    assert rec["steps"][2]["outcome"] == "refused" and rec["mission_classification"] == COMPENSATED
    assert env.stack.count("twenty", "POST") == 0 and env.stack.quotations.keys() == {"SAL-QTN-2026-00001"}
