"""WAITING_RECONCILIATION: an unknown outcome parks the mission durably and the same mission resumes after reconciliation.

#553 made a dispatched reservation un-sendable-twice (at-most-once). That left the mission FAILED, which is safe but not
recoverable. These tests pin both halves for good: the effect is never sent twice, and uncertainty is never a terminal failure.
They reproduce the three-way comparison from the review (no guard / before the fix / the fix) so a future change cannot restore
either duplicate execution or terminal failure."""
from __future__ import annotations

import threading
import time

import pytest

from agentic_os.mission.authority import (
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, SqliteEnvelopeStore, SqliteLedger)
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.policy import STANDING, Effect, MissionPolicy, PolicyRule
from agentic_os.mission.reconcile import ReconciliationConflict, ReconciliationError
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState, NodeState)

NOW = time.time()
EVIDENCE = {"kind": "provider_record", "reference": "provider dashboard, payment pi_123, 2026-10-24 09:14"}


class Crash(BaseException):
    """The process dies. Not an Exception, so nothing records a failure: exactly what a kill -9 leaves behind."""


class Provider:
    """The EXTERNAL system. It applies each effect at most once per key, and counts every handler call (a call is not an effect)."""

    def __init__(self):
        self.effects: dict[str, int] = {}
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def apply(self, key):
        with self.lock:
            self.effects.setdefault(key, 0)
            self.effects[key] += 1


class Plan:
    """first -> second -> third, three side-effecting steps; `third` reads what `second` produced when `chain` is set."""

    def __init__(self, chain=False):
        self.chain = chain

    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=[
            IntentStep(outcome="done1", need="first"), IntentStep(outcome="done2", need="second"),
            IntentStep(outcome="done3", need="third", inputs_from=["done2"] if self.chain else [])])


def registry():
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [
        CapabilitySpec("op.first", "op", provides=["done1"], side_effecting=True, undo="op.undo"),
        CapabilitySpec("op.second", "op", provides=["done2"], side_effecting=True, undo="op.undo"),
        CapabilitySpec("op.third", "op", provides=["done3"], side_effecting=True, undo="op.undo"),
        CapabilitySpec("op.undo", "op", side_effecting=True)]))
    return reg


def handlers(p: Provider, mode: str):
    """mode: ok | crash_before_effect | crash_after_effect | error_after_send | error_not_applied"""
    def first(i):
        p.calls.append("first"); p.apply("first"); return {"ok": 1}

    def second(i):
        p.calls.append("second")
        if mode == "crash_before_effect":
            raise Crash()
        if mode == "crash_after_effect":
            p.apply("second"); raise Crash()
        if mode == "error_after_send":
            p.apply("second"); raise TimeoutError("timed out after the request was written")
        if mode == "error_not_applied":
            err = RuntimeError("rejected before sending"); err.not_applied = True; raise err
        p.apply("second"); return {"ok": 2, "provider_object_id": "pi_second"}

    def third(i):
        p.calls.append("third"); p.apply("third"); return {"ok": 3}
    return {"op.first": first, "op.second": second, "op.third": third, "op.undo": lambda i: {"undone": True}}


class World:
    """A durable process: the same event log and authority ledger across 'restarts'."""

    def __init__(self, tmp, monkeypatch, provider=None, guard=True, chain=False):
        monkeypatch.setenv("AGENTIC_OS_PROFILE", "test")
        self.tmp, self.provider, self.guard_on, self.chain = tmp, provider or Provider(), guard, chain
        self.log = str(tmp / "events.jsonl")
        self.db = str(tmp / "auth.db")
        if guard:
            st = SqliteEnvelopeStore(self.db)
            if st.get("env") is None:
                st.put(AuthorityEnvelope(tenant="acme", principal="o", mission_id="env", issued_at=NOW - 10, expires_at=NOW + 100_000,
                                         grants=(Grant("op.*"),), budget=AutonomyBudget(usd=1000, max_actions=1000)))

    def boot(self, mode="ok") -> MissionRuntime:
        kw = {}
        if self.guard_on:
            kw = dict(authority=AuthorityGuard(SqliteEnvelopeStore(self.db), SqliteLedger(self.db + ".usage")),
                      authority_key=lambda m: "env")
        return MissionRuntime(registry(), Executor(InMemoryOperatorClient(handlers(self.provider, mode))),
                              store=EventStore(self.log), planner=Plan(self.chain), **kw)

    def new_mission(self, rt):
        pol = MissionPolicy(id="p", version="1", rules=(
            PolicyRule("w", Effect.REQUIRE_APPROVAL, "side_effecting", approval_mode=STANDING),)) if self.guard_on else None
        return rt.create_mission("three steps", policy_refs=["*"], tenant="acme", principal="o", policy=pol)

    def restart(self, mid, mode="ok") -> MissionRuntime:
        rt = self.boot(mode)
        rt.rehydrate(mid)
        return rt


def crash_then_restart(tmp, monkeypatch, *, guard=True, mode="crash_before_effect", chain=False):
    w = World(tmp, monkeypatch, guard=guard, chain=chain)
    rt = w.boot(mode)
    m = w.new_mission(rt)
    with pytest.raises(Crash):
        rt.run(m.id)
    w.provider.calls_at_crash = list(w.provider.calls)
    w.provider.calls.clear()
    rt2 = w.restart(m.id)                                    # the process is gone; a new one starts over the same durable state
    return w, m.id, rt2


def events(rt, mid, typ):
    return [e.payload for e in rt.store.for_mission(mid) if e.type == typ]


def lease_states(rt, mission_id):
    return {d["lease_id"]: d["state"] for d in rt.authority.outstanding(detail=True)} if rt.authority else {}


# ── the three-way comparison from the review ─────────────────────────────────────────────
def test_three_way_comparison_no_guard_fix_and_negative_control(tmp_path, monkeypatch):
    # 1. no guard (development default): the in-flight step is resumed and the mission finishes. Completed work is not rerun.
    (tmp_path / "a").mkdir()
    w, mid, rt = crash_then_restart(tmp_path / "a", monkeypatch, guard=False)
    out = rt.run(mid)
    assert w.provider.calls == ["second", "third"] and out.state == MissionState.SUCCEEDED

    # 2. the fix: with a guard, a restart does NOT send the in-flight step again, and the mission is PARKED, not failed.
    (tmp_path / "b").mkdir()
    w, mid, rt = crash_then_restart(tmp_path / "b", monkeypatch, guard=True)
    out = rt.run(mid)
    assert w.provider.calls == []                                                   # never sent again
    assert out.state == MissionState.WAITING_RECONCILIATION and out.state != MissionState.FAILED
    assert "first" not in w.provider.calls                                          # completed work is not rerun
    assert w.provider.effects["first"] == 1

    # 3. negative control: if a change ever restored the old behaviour (re-dispatching a dispatched reservation), this harness
    #    would see the duplicate. Proves the assertions above can actually fail.
    (tmp_path / "c").mkdir()
    w, mid, rt = crash_then_restart(tmp_path / "c", monkeypatch, guard=True)
    rt._lease_of = lambda m, nid: None                                              # "the runtime forgot the reservation"
    out = rt.run(mid)
    assert w.provider.calls == ["second", "third"]                                  # the duplicate is visible to the harness


# ── 1 + 2: parked durably, with everything a resume needs ────────────────────────────────
def test_the_mission_is_parked_with_node_attempt_reservation_and_completed_state(tmp_path, monkeypatch):
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch)
    rt.run(mid)
    (req,) = events(rt, mid, "ReconciliationRequired")
    node = next(n for n in rt._plans[mid].graph.nodes if n.id == req["node_id"])
    assert req["capability"] == "op.second" and req["attempt"] == 1 and req["lease_id"] == f"{mid}:{node.id}:a1"
    assert req["idempotency_key"] and rt.authority.lease(req["lease_id"])["state"] in ("dispatched", "unknown")
    first = rt._plans[mid].graph.nodes[0]
    assert rt.repo.node_status(mid)[first.id] == NodeState.DONE and rt.repo.node_status(mid)[node.id] == NodeState.RUNNING
    assert not events(rt, mid, "NodeFailed") and not events(rt, mid, "NodeCompensated")      # nothing declared failed, nothing undone


def test_the_park_survives_repeated_restarts_and_nothing_runs_while_parked(tmp_path, monkeypatch):
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch)
    rt.run(mid)
    for _ in range(3):                                                              # restart, again and again
        rt = w.restart(mid)
        assert rt._missions[mid].state == MissionState.WAITING_RECONCILIATION
        assert rt.run(mid).state == MissionState.WAITING_RECONCILIATION
        assert rt.run(mid).state == MissionState.WAITING_RECONCILIATION
    assert w.provider.calls == []
    (pend,) = rt.repo.pending_reconciliation(mid)
    node = next(n for n in rt._plans[mid].graph.nodes if n.id == pend["node_id"])
    assert node.attempts == 1                                                       # the attempt number survived every restart
    assert [i["kind"] for i in rt.inbox() if i["mission_id"] == mid] == ["reconciliation"]
    assert rt.inbox()[0]["lease_id"] == pend["lease_id"]


def test_running_a_parked_mission_is_a_no_op_that_writes_nothing(tmp_path, monkeypatch):
    """While parked, run() must not re-evaluate gates or re-record the refusal: the log does not grow, however often it is called
    (and even if something overwrote the mission state back to RUNNING)."""
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch)
    rt.run(mid)
    before = len(rt.store.for_mission(mid))
    for _ in range(5):
        assert rt.run(mid).state == MissionState.WAITING_RECONCILIATION
    rt._set_state(rt._missions[mid], MissionState.RUNNING)                              # a state overwrite, as a bug or a race might do
    before += 1                                                                          # (that one MissionStateChanged is ours)
    assert rt.run(mid).state == MissionState.WAITING_RECONCILIATION
    after = rt.store.for_mission(mid)
    kinds = [e.type for e in after[before - 1:]]
    assert kinds == ["MissionStateChanged"] or kinds == ["MissionStateChanged", "MissionStateChanged"], kinds   # only the state restores itself
    assert len(events(rt, mid, "ReconciliationRequired")) == 1 and len(events(rt, mid, "RedispatchRefused")) == 1
    assert w.provider.calls == []


def test_suspend_and_resume_cannot_bypass_the_park(tmp_path, monkeypatch):
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch)
    rt.run(mid)
    rt.suspend(mid, actor="someone", reason="pause it")
    assert rt.resume(mid, actor="someone").state == MissionState.WAITING_RECONCILIATION
    assert w.provider.calls == []


def test_a_side_effecting_call_that_errors_after_it_may_have_been_sent_parks_instead_of_failing(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt = w.boot("error_after_send")
    m = w.new_mission(rt)
    out = rt.run(m.id)
    assert out.state == MissionState.WAITING_RECONCILIATION
    assert not events(rt, m.id, "NodeFailed") and not events(rt, m.id, "NodeCompensated")      # no failure declared, no undo of step 1
    assert w.provider.effects["first"] == 1 and w.provider.effects["second"] == 1               # the effect DID happen (provider side)
    (req,) = events(rt, m.id, "ReconciliationRequired")
    assert "TimeoutError" in req["reason"] and rt.authority.lease(req["lease_id"])["state"] == "unknown"


def test_an_error_that_proves_nothing_was_applied_still_fails_normally(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    rt = w.boot("error_not_applied")
    m = w.new_mission(rt)
    out = rt.run(m.id)
    assert out.state == MissionState.FAILED                                         # unchanged behaviour: it provably did not happen
    assert not events(rt, m.id, "ReconciliationRequired")


# ── 3 + 4: authenticated, evidence-backed reconciliation, and resume of the SAME mission ──
def test_not_executed_releases_the_reservation_and_the_same_mission_resumes_without_rerunning_completed_work(tmp_path, monkeypatch):
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch, mode="crash_before_effect")
    rt.run(mid)
    assert w.provider.effects.get("second") is None                                 # the provider has nothing: the call never landed
    out = rt.reconcile_node(mid, rt.repo.pending_reconciliation(mid)[0]["node_id"], executed=False, evidence=EVIDENCE,
                            actor="approver:acme", reason="provider shows no charge for this key")
    assert out.id == mid and out.state == MissionState.SUCCEEDED
    assert w.provider.calls == ["second", "third"] and "first" not in w.provider.calls          # completed work is not rerun
    assert w.provider.effects == {"first": 1, "second": 1, "third": 1}                          # every effect exactly once
    states = lease_states(rt, mid)
    assert rt.authority.outstanding() == []
    (res,) = events(rt, mid, "ReconciliationResolved")
    assert res["actor"] == "approver:acme" and res["evidence"]["kind"] == "provider_record" and res["executed"] is False
    assert rt.authority.lease(res["lease_id"])["state"] == "released"
    assert rt.authority.lease(res["lease_id"])["meta"]["resolved_by"] == "approver:acme"


def test_executed_settles_it_marks_the_node_done_without_calling_the_handler_and_the_mission_continues(tmp_path, monkeypatch):
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch, mode="crash_after_effect")
    rt.run(mid)
    assert w.provider.effects["second"] == 1                                        # the effect really happened before the crash
    node_id = rt.repo.pending_reconciliation(mid)[0]["node_id"]
    out = rt.reconcile_node(mid, node_id, executed=True, evidence={**EVIDENCE, "provider_ref": "pi_second"}, actor="admin",
                            reason="charge pi_second is on the provider dashboard")
    assert out.state == MissionState.SUCCEEDED
    assert w.provider.calls == ["third"]                                            # NOT second again, NOT first again
    assert w.provider.effects == {"first": 1, "second": 1, "third": 1}
    done = [e.payload for e in rt.store.for_mission(mid) if e.type == "NodeSucceeded" and e.payload.get("reconciled")]
    assert done and done[0]["node_id"] == node_id and done[0]["result"]["reconciled_by"] == "admin"
    lease = rt.authority.lease(events(rt, mid, "ReconciliationResolved")[0]["lease_id"])
    assert lease["state"] == "settled" and lease["meta"]["provider_ref"] == "pi_second"


def test_a_reconciled_node_that_later_steps_read_needs_the_providers_real_result(tmp_path, monkeypatch):
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch, mode="crash_after_effect", chain=True)
    rt.run(mid)
    node_id = rt.repo.pending_reconciliation(mid)[0]["node_id"]
    with pytest.raises(ReconciliationError, match="supply the provider's real result"):
        rt.reconcile_node(mid, node_id, executed=True, evidence=EVIDENCE, actor="admin", reason="it is on the dashboard")
    assert rt.repo.pending_reconciliation(mid) and rt.authority.lease(rt.repo.pending_reconciliation(mid)[0]["lease_id"])["state"] \
        in ("dispatched", "unknown")                                                # refused BEFORE anything changed
    out = rt.reconcile_node(mid, node_id, executed=True, evidence=EVIDENCE, actor="admin", reason="it is on the dashboard",
                            result={"charge": "pi_second", "amount": 5})
    assert out.state == MissionState.SUCCEEDED
    assert rt._world(mid).get("done2")["charge"] == "pi_second"                    # later steps read the real result, not a placeholder


def test_resume_false_leaves_the_mission_paused_for_the_owner(tmp_path, monkeypatch):
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch)
    rt.run(mid)
    out = rt.reconcile_node(mid, rt.repo.pending_reconciliation(mid)[0]["node_id"], executed=False, evidence=EVIDENCE, actor="admin",
                            reason="provider shows nothing", resume=False)
    assert out.state == MissionState.PAUSED and w.provider.calls == []
    assert rt.resume(mid, actor="admin").state == MissionState.SUCCEEDED and w.provider.calls == ["second", "third"]


@pytest.mark.parametrize("evidence", [
    None, {}, {"kind": "provider_record"}, {"reference": "abcd1234"}, {"kind": "hearsay", "reference": "someone said so"},
    {"kind": "provider_record", "reference": "x"}, {"kind": "other", "reference": "abcd1234", "note": "short"}, "I checked",
])
def test_a_bare_assertion_is_not_evidence(tmp_path, monkeypatch, evidence):
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch)
    rt.run(mid)
    node_id = rt.repo.pending_reconciliation(mid)[0]["node_id"]
    with pytest.raises(ReconciliationError):
        rt.reconcile_node(mid, node_id, executed=False, evidence=evidence, actor="admin", reason="provider shows nothing")
    assert rt._missions[mid].state == MissionState.WAITING_RECONCILIATION and w.provider.calls == []


@pytest.mark.parametrize("kw", [{"actor": ""}, {"actor": "  "}, {"reason": ""}, {"reason": "ok"}])
def test_the_person_and_a_reason_are_required(tmp_path, monkeypatch, kw):
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch)
    rt.run(mid)
    args = dict(executed=False, evidence=EVIDENCE, actor="admin", reason="provider shows nothing")
    args.update(kw)
    with pytest.raises(ReconciliationError):
        rt.reconcile_node(mid, rt.repo.pending_reconciliation(mid)[0]["node_id"], **args)
    assert w.provider.calls == [] and rt._missions[mid].state == MissionState.WAITING_RECONCILIATION


def test_only_a_node_that_is_actually_parked_can_be_reconciled(tmp_path, monkeypatch):
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch)
    rt.run(mid)
    first = rt._plans[mid].graph.nodes[0]
    for nid in ("no-such-node", first.id):
        with pytest.raises(ReconciliationError):
            rt.reconcile_node(mid, nid, executed=True, evidence=EVIDENCE, actor="admin", reason="provider shows it done")
    assert w.provider.calls == []


# ── 5: concurrent reconciliation and resume cannot duplicate execution ───────────────────
def test_the_same_decision_twice_is_idempotent_and_the_opposite_decision_is_a_conflict(tmp_path, monkeypatch):
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch)
    rt.run(mid)
    node_id = rt.repo.pending_reconciliation(mid)[0]["node_id"]
    args = dict(evidence=EVIDENCE, actor="admin", reason="provider shows nothing for this key")
    assert rt.reconcile_node(mid, node_id, executed=False, **args).state == MissionState.SUCCEEDED
    assert w.provider.calls == ["second", "third"]
    assert rt.reconcile_node(mid, node_id, executed=False, **args).state == MissionState.SUCCEEDED     # a client retry: converges
    assert w.provider.calls == ["second", "third"]                                                       # ... and sends nothing
    with pytest.raises(ReconciliationConflict):
        rt.reconcile_node(mid, node_id, executed=True, **args)
    assert w.provider.effects == {"first": 1, "second": 1, "third": 1}


@pytest.mark.parametrize("round_", range(8))
def test_concurrent_reconcile_and_resume_attempts_execute_each_step_at_most_once(tmp_path, monkeypatch, round_):
    d = tmp_path / f"r{round_}"
    d.mkdir()
    w, mid, rt = crash_then_restart(d, monkeypatch, mode="crash_before_effect")
    rt.run(mid)
    node_id = rt.repo.pending_reconciliation(mid)[0]["node_id"]
    errors, results = [], []
    barrier = threading.Barrier(11)
    args = dict(evidence=EVIDENCE, actor="admin", reason="provider shows nothing for this key")

    def reconcile(executed):
        barrier.wait()
        try:
            results.append(rt.reconcile_node(mid, node_id, executed=executed, **args).state)
        except (ReconciliationConflict, ReconciliationError) as e:
            errors.append(type(e).__name__)

    def run():
        barrier.wait()
        results.append(rt.run(mid).state)
    ts = ([threading.Thread(target=reconcile, args=(False,)) for _ in range(4)]
          + [threading.Thread(target=reconcile, args=(True,)) for _ in range(2)]
          + [threading.Thread(target=run) for _ in range(5)])
    [t.start() for t in ts]
    [t.join() for t in ts]
    # whichever resolver won (the ledger is first-wins), no step ran twice and nothing ran that the winner did not authorise
    assert all(w.provider.calls.count(s) <= 1 for s in ("second", "third")), w.provider.calls
    assert "first" not in w.provider.calls
    assert all(v <= 1 for v in w.provider.effects.values())
    assert rt._missions[mid].state in (MissionState.SUCCEEDED, MissionState.WAITING_RECONCILIATION)
    final = events(rt, mid, "ReconciliationResolved")
    assert len(final) == 1 and final[0]["executed"] in (True, False)                 # exactly one resolution recorded
    if final[0]["executed"] is False:
        assert w.provider.calls == ["second", "third"] and rt._missions[mid].state == MissionState.SUCCEEDED
    else:
        assert w.provider.calls == ["third"] and rt._missions[mid].state == MissionState.SUCCEEDED


def test_two_processes_over_the_same_ledger_cannot_both_dispatch_the_same_reservation(tmp_path, monkeypatch):
    """Replicas: the ledger's atomic dispatch (one winner) is what keeps a resolved reservation to a single send."""
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch)
    rt.run(mid)
    pend = rt.repo.pending_reconciliation(mid)[0]
    rt.authority.reconcile(pend["lease_id"], executed=False)                         # resolved in the ledger
    a = AuthorityGuard(SqliteEnvelopeStore(w.db), SqliteLedger(w.db + ".usage"))
    b = AuthorityGuard(SqliteEnvelopeStore(w.db), SqliteLedger(w.db + ".usage"))
    from agentic_os.mission.authority import Action
    wins = []
    barrier = threading.Barrier(8)

    def go(g):
        barrier.wait()
        d = g.authorize("env", "acme", Action("op.second", {}, 1, True), lease_id=pend["lease_id"])
        wins.append(d.allowed and g.dispatch_once(pend["lease_id"]))
    ts = [threading.Thread(target=go, args=(g,)) for g in (a, b) * 4]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(wins) == 1                                                             # one dispatch, however many replicas race


# ── authenticated API ────────────────────────────────────────────────────────────────────
@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agentic_os.mission.api import build_router
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "admin-tok")
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", "acme=req-acme,beta=req-beta")
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", "acme=appr-acme,beta=appr-beta")
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch)
    rt.run(mid)
    app = FastAPI()
    app.include_router(build_router(rt))
    return w, mid, rt, TestClient(app, raise_server_exceptions=False)


def hdr(t):
    return {"Authorization": f"Bearer {t}"}


def body(rt, mid, **kw):
    b = {"node_id": rt.repo.pending_reconciliation(mid)[0]["node_id"], "outcome": "not_executed", "evidence": EVIDENCE,
         "reason": "provider shows no charge for this key"}
    b.update(kw)
    return b


def test_api_credentials_matrix(api):
    w, mid, rt, c = api
    url = f"/missions/{mid}/reconcile"
    assert c.post(url, json=body(rt, mid)).status_code in (401, 503)                 # no credential
    assert c.post(url, json=body(rt, mid), headers=hdr("req-acme")).status_code == 403   # a REQUESTER can never reconcile
    assert c.post(url, json=body(rt, mid), headers=hdr("appr-beta")).status_code == 404  # another tenant's approver: no existence leak
    assert c.post(url, json=body(rt, mid), headers=hdr("wrong")).status_code == 401
    assert w.provider.calls == [] and rt._missions[mid].state == MissionState.WAITING_RECONCILIATION   # none of those changed anything


def test_api_an_approver_reconciles_resumes_and_the_actor_comes_from_the_credential(api):
    w, mid, rt, c = api
    r = c.post(f"/missions/{mid}/reconcile", json={**body(rt, mid), "actor": "someone-else"}, headers=hdr("appr-acme"))
    assert r.status_code == 200 and r.json()["state"] == "succeeded" and r.json()["pending_reconciliation"] == []
    (res,) = events(rt, mid, "ReconciliationResolved")
    assert res["actor"] == "approver:acme"                                            # the credential, never the body
    assert w.provider.calls == ["second", "third"] and w.provider.effects == {"first": 1, "second": 1, "third": 1}


def test_api_admin_can_and_evidence_is_mandatory_and_conflicts_are_409(api):
    w, mid, rt, c = api
    url = f"/missions/{mid}/reconcile"
    assert c.post(url, json=body(rt, mid, evidence={}), headers=hdr("admin-tok")).status_code == 422
    assert c.post(url, json=body(rt, mid, reason=""), headers=hdr("admin-tok")).status_code == 422
    assert c.post(url, json=body(rt, mid, outcome="maybe"), headers=hdr("admin-tok")).status_code == 422
    assert c.get(f"/missions/{mid}", headers=hdr("admin-tok")).json()["pending_reconciliation"][0]["capability"] == "op.second"
    b = body(rt, mid)
    assert c.post(url, json=b, headers=hdr("admin-tok")).status_code == 200
    assert c.post(url, json=b, headers=hdr("admin-tok")).status_code == 200            # the same decision again converges
    assert c.post(url, json={**b, "outcome": "executed"}, headers=hdr("admin-tok")).status_code == 409
    assert w.provider.calls == ["second", "third"]


def test_api_with_no_credentials_configured_is_disabled_and_changes_nothing(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agentic_os.mission.api import build_router
    for k in ("AGENTIC_OS_CONTROL_TOKEN", "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    w, mid, rt = crash_then_restart(tmp_path, monkeypatch)
    rt.run(mid)
    app = FastAPI()
    app.include_router(build_router(rt))
    r = TestClient(app, raise_server_exceptions=False).post(f"/missions/{mid}/reconcile", json=body(rt, mid))
    assert r.status_code == 503 and w.provider.calls == [] and rt._missions[mid].state == MissionState.WAITING_RECONCILIATION


# ── a concurrent wave: one node parks, its siblings' completed work is kept ───────────────
def test_in_a_concurrent_wave_the_siblings_results_are_committed_when_one_node_parks(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    kw = dict(authority=AuthorityGuard(SqliteEnvelopeStore(w.db), SqliteLedger(w.db + ".usage")), authority_key=lambda m: "env")
    rt = MissionRuntime(registry(), Executor(InMemoryOperatorClient(handlers(w.provider, "error_after_send"))),
                        store=EventStore(w.log), planner=Plan(), max_concurrency=3, **kw)
    m = w.new_mission(rt)
    out = rt.run(m.id)
    assert out.state == MissionState.WAITING_RECONCILIATION
    done = {e.payload["capability"] for e in rt.store.for_mission(m.id) if e.type == "NodeSucceeded"}
    assert done == {"op.first", "op.third"}                                          # the siblings that finished are recorded as done
    assert w.provider.effects == {"first": 1, "second": 1, "third": 1}
    node_id = rt.repo.pending_reconciliation(m.id)[0]["node_id"]
    w.provider.calls.clear()
    res = rt.reconcile_node(m.id, node_id, executed=True, evidence=EVIDENCE, actor="admin", reason="the charge is on the dashboard")
    assert res.state == MissionState.SUCCEEDED and w.provider.calls == []            # nothing at all is sent again
    assert w.provider.effects == {"first": 1, "second": 1, "third": 1}
