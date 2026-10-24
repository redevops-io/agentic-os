"""Hard limits in the serial loop, durable lease records, and reconciliation that never turns uncertainty into permission to repeat."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _fork_support import CountingOperator, build, spec                      # noqa: E402

from agentic_os.mission.authority import (                                   # noqa: E402
    Action, AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, Lease, MemoryEnvelopeStore, MemoryLedger,
    SqliteEnvelopeStore, SqliteLedger, provider_ref_of)
from agentic_os.mission.executor import Executor, InMemoryOperatorClient     # noqa: E402
from agentic_os.mission.policy import STANDING, Effect, MissionPolicy, PolicyRule   # noqa: E402
from agentic_os.mission.reconcile import Finding, Outcome, Reconciler, idempotency_probe   # noqa: E402
from agentic_os.mission.registry import CapabilityRegistry                   # noqa: E402
from agentic_os.mission.runtime import MissionRuntime                        # noqa: E402
from agentic_os.mission.types import (                                       # noqa: E402
    Budget, CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState)

NOW = 6_000_000.0


def events(rt, mid, typ):
    return [e.payload for e in rt.store.for_mission(mid) if e.type == typ]


# ── hard limits in the serial loop ───────────────────────────────────────────────────────
def test_the_default_budget_does_not_change_a_normal_run():
    rt, op, m = build()
    assert rt.run(m.id).state in (MissionState.SUCCEEDED, MissionState.WAITING_HUMAN)
    assert not events(rt, m.id, "HardLimitReached")


def test_a_spend_cap_stops_the_serial_loop_before_the_wave_that_would_pass_it():
    rt, op, m = build()
    m.budget = Budget(usd=0.0001)                      # tighten AFTER planning: the plan-time check already passed
    out = rt.run(m.id)
    assert out.state == MissionState.FAILED and out.outcome["blocked"] == "hard_limit" and out.outcome["limit"] == "usd"
    ev = events(rt, m.id, "HardLimitReached")[0]
    assert ev["limit"] == "usd" and ev["used"] > ev["cap"]


def test_a_token_cap_stops_the_loop_before_the_call_that_would_pass_it():
    from agentic_os.mission.types import NodeCost
    calls = []
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [CapabilitySpec("op.gen", "op", provides=["done"], cost=NodeCost(tokens=500))]))
    rt = MissionRuntime(reg, Executor(InMemoryOperatorClient({"op.gen": lambda i: (calls.append(i), {"ok": True})[1]})), planner=_P())
    m = rt.create_mission("gen", policy_refs=["*"], budget=Budget(usd=100.0, tokens=100))      # the plan-time check ignores tokens
    out = rt.run(m.id)
    assert out.state == MissionState.FAILED and out.outcome["limit"] == "tokens" and out.outcome["used"] == 500
    assert calls == []                                                                            # never dispatched
    m2 = rt.create_mission("gen", policy_refs=["*"], budget=Budget(usd=100.0, tokens=1000))
    assert rt.run(m2.id).state == MissionState.SUCCEEDED and len(calls) == 1                       # under the cap it runs


def test_a_deadline_in_the_past_stops_before_anything_is_dispatched():
    rt, op, m = build()
    m.deadline = time.time() - 1
    out = rt.run(m.id)
    assert out.state == MissionState.FAILED and out.outcome["limit"] == "deadline"
    assert not events(rt, m.id, "NodeDispatched")


def test_the_wall_clock_budget_stops_the_loop_and_counts_active_run_time():
    rt, op, m = build()
    m.budget = Budget(usd=1000.0, wall_clock_s=0.000001)
    time.sleep(0.01)
    # the segment starts when run() starts, so a mission that WAITED (e.g. parked for a human) is not charged for the wait
    out = rt.run(m.id)
    assert out.outcome["limit"] == "wall_clock_s" and not events(rt, m.id, "NodeDispatched")


def test_a_hard_limit_is_a_reasoned_stop_not_a_silent_one():
    rt, op, m = build()
    m.budget = Budget(usd=0.0001)
    out = rt.run(m.id)
    assert {"blocked", "limit", "used", "cap"} <= set(out.outcome)
    kinds = [e.type for e in rt.store.for_mission(m.id)]
    assert kinds.index("HardLimitReached") < len(kinds) - 1 - kinds[::-1].index("MissionStateChanged")   # the reason precedes the final state


# ── durable lease records ────────────────────────────────────────────────────────────────
def guard_with(*grants, ledger=None, store=None, clock=None):
    st = store or MemoryEnvelopeStore()
    st.put(AuthorityEnvelope(tenant="acme", principal="o", mission_id="env", issued_at=NOW - 10, expires_at=NOW + 10_000,
                             grants=grants, budget=AutonomyBudget(usd=1000, max_actions=1000)))
    return AuthorityGuard(st, ledger or MemoryLedger(), clock=clock or (lambda: NOW))


def make_lease(g, lid, cap="pay.send", **meta):
    d = g.authorize("env", "acme", Action(cap, {"amount": 5}, 1, True), lease_id=lid,
                    lease_meta={"mission_id": "m-1", "node_id": "n-1", "attempt": 1, "idempotency_key": f"idem-{lid}", **meta})
    assert d.allowed, d
    return d


@pytest.fixture(params=["memory", "sqlite"])
def ledger(request, tmp_path):
    return MemoryLedger() if request.param == "memory" else SqliteLedger(str(tmp_path / "u.db"))


def test_a_reservation_records_who_and_what(ledger):
    g = guard_with(Grant("pay.send", max_tier=1), ledger=ledger)
    make_lease(g, "L1")
    rec = g.lease("L1")
    assert rec["state"] == "reserved" and rec["meta"]["capability"] == "pay.send" and rec["meta"]["tenant"] == "acme"
    assert rec["meta"]["mission_id"] == "m-1" and rec["meta"]["attempt"] == 1 and rec["meta"]["idempotency_key"] == "idem-L1"
    assert rec["meta"]["envelope_digest"].startswith("sha256:") and rec["meta"]["side_effecting"] is True
    g.dispatch_once("L1")
    g.annotate("L1", dispatched_at=NOW, execution_id="exec-1", provider_ref=None)       # None fields are not recorded
    meta = g.lease("L1")["meta"]
    assert meta["execution_id"] == "exec-1" and "provider_ref" not in meta
    assert g.outstanding(detail=True)[0]["lease_id"] == "L1" and g.lease("missing") is None
    assert g.annotate("missing", x=1) is False


def test_lease_records_survive_a_restart_and_old_databases_are_migrated(tmp_path):
    import sqlite3
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)                                   # a ledger written before lease records existed
    c.execute("CREATE TABLE leases (lease_id TEXT PRIMARY KEY, k TEXT NOT NULL, deltas TEXT NOT NULL, state TEXT NOT NULL, "
              "rate_name TEXT, rate_ts REAL, updated REAL NOT NULL)")
    c.execute("INSERT INTO leases VALUES ('old','env','{\"actions\":1}','dispatched',NULL,NULL,1.0)")
    c.commit(); c.close()
    led = SqliteLedger(p)                                    # opens and migrates
    assert led.lease_get("old")["state"] == "dispatched" and led.lease_get("old")["meta"] == {}
    assert led.lease_annotate("old", reconcile_status="needs_review")
    again = SqliteLedger(p)
    assert again.lease_get("old")["meta"]["reconcile_status"] == "needs_review"


def test_provider_reference_is_taken_only_from_conventional_keys():
    assert provider_ref_of({"provider_object_id": "inv_9"}) == "inv_9"
    assert provider_ref_of({"provider_ref": 42}) == "42"
    assert provider_ref_of({"id": "x"}) == "" and provider_ref_of("nope") == "" and provider_ref_of(None) == ""


# ── the reconciler ───────────────────────────────────────────────────────────────────────
def recon(g, probes=None, age=0, **kw):
    """A reconciler whose clock is `age` seconds after the reservations were made."""
    events_ = []
    return Reconciler(g, probes, clock=lambda: NOW + age, on_event=events_.append, **kw), events_


def test_production_releases_a_reservation_that_was_never_dispatched_after_the_authorization_lifetime(ledger, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    g = guard_with(Grant("pay.send", max_calls=1), ledger=ledger)
    make_lease(g, "L1")
    young, _ = recon(g, age=60)
    assert young.run_once().kept == ["L1"] and g.lease("L1")["state"] == "reserved"          # too young: an authorization could still be live
    old, ev = recon(g, age=2000)
    rep = old.run_once()
    assert rep.released == ["L1"] and g.lease("L1")["state"] == "released"
    assert g.lease("L1")["meta"]["reconcile_status"] == "released_never_dispatched" and ev[0]["type"] == "LEASE_RELEASED"
    make_lease(g, "L2")                                                                       # capacity came back (max_calls=1)


def test_outside_production_a_reserved_lease_needs_a_probe_before_it_is_released(ledger, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    g = guard_with(Grant("pay.send"), ledger=ledger)
    make_lease(g, "L1")
    r, _ = recon(g, age=99999)
    rep = r.run_once()
    assert rep.kept == ["L1"] and g.lease("L1")["state"] == "reserved"                        # a caller may have used it without marking it
    r.register("pay.*", lambda lease: Finding(Outcome.NOT_EXECUTED, reason="provider has nothing"))
    assert r.run_once().released == ["L1"] and g.lease("L1")["state"] == "released"


def test_dispatched_with_no_independent_lookup_stays_counted_and_goes_to_a_human(ledger):
    g = guard_with(Grant("pay.send", max_calls=1), ledger=ledger)
    make_lease(g, "L1")
    g.dispatch_once("L1")
    r, ev = recon(g)
    rep = r.run_once()
    assert rep.kept == ["L1"] and rep.settled == [] and rep.released == []
    assert rep.needs_review[0]["capability"] == "pay.send" and "no independent lookup" in rep.needs_review[0]["reason"]
    assert g.lease("L1")["state"] == "dispatched" and g.lease("L1")["meta"]["reconcile_status"] == "needs_review"
    assert ev[0]["type"] == "UNKNOWN_OUTCOME"
    assert g.authorize("env", "acme", Action("pay.send", {"amount": 1}, 1, True), lease_id="L2").verdict.value == "escalate"   # still counted


def test_a_probe_that_confirms_the_effect_settles_it_with_the_provider_reference(ledger):
    g = guard_with(Grant("pay.send"), ledger=ledger)
    make_lease(g, "L1")
    g.dispatch_once("L1"); g.unknown("L1")
    r, ev = recon(g, {"pay.*": lambda lease: Finding(Outcome.EXECUTED, provider_ref="pi_123", reason="found")})
    rep = r.run_once()
    rec = g.lease("L1")
    assert rep.settled == ["L1"] and rec["state"] == "settled" and rec["meta"]["provider_ref"] == "pi_123"
    assert rec["meta"]["reconcile_status"] == "settled_by_probe" and ev[0]["type"] == "LEASE_SETTLED"
    assert r.run_once().summary() == {"settled": 0, "released": 0, "kept": 0, "needs_review": 0}     # idempotent: nothing left


def test_a_probe_that_proves_it_did_not_happen_returns_the_capacity(ledger):
    g = guard_with(Grant("pay.send", max_calls=1), ledger=ledger)
    make_lease(g, "L1")
    g.dispatch_once("L1"); g.unknown("L1")
    r, _ = recon(g, {"pay.*": lambda lease: Finding(Outcome.NOT_EXECUTED, reason="authoritatively absent")})
    assert r.run_once().released == ["L1"] and g.lease("L1")["state"] == "released"
    make_lease(g, "L2")                                                                       # the one allowed call is available again


@pytest.mark.parametrize("probe", [
    lambda lease: Finding(Outcome.UNKNOWN, reason="provider could not say"),
    lambda lease: (_ for _ in ()).throw(RuntimeError("provider timeout")),
    lambda lease: "not a finding",
])
def test_unknown_probe_answers_and_failures_never_resolve_anything(ledger, probe):
    g = guard_with(Grant("pay.send"), ledger=ledger)
    make_lease(g, "L1")
    g.dispatch_once("L1"); g.unknown("L1")
    r, _ = recon(g, {"pay.*": probe})
    rep = r.run_once()
    assert rep.kept == ["L1"] and len(rep.needs_review) == 1 and g.lease("L1")["state"] == "unknown"
    assert g.lease("L1")["meta"]["reconcile_attempts"] == 1
    r.run_once()
    assert g.lease("L1")["meta"]["reconcile_attempts"] == 2


def test_contradictory_evidence_is_never_resolved_in_favour_of_repeating(ledger):
    g = guard_with(Grant("pay.send"), ledger=ledger)
    make_lease(g, "L1")
    g.dispatch_once("L1")
    g.annotate("L1", provider_ref="pi_999")                                                   # we HAVE a provider reference ...
    r, _ = recon(g, {"pay.*": lambda lease: Finding(Outcome.NOT_EXECUTED, reason="not found")})   # ... but a probe says absent
    rep = r.run_once()
    assert rep.released == [] and rep.needs_review and "contradictory" in rep.needs_review[0]["reason"]
    assert g.lease("L1")["state"] == "dispatched"


def test_idempotency_probe_treats_not_found_as_absent_only_for_an_authoritative_provider():
    lease = {"meta": {"idempotency_key": "k1"}}
    assert idempotency_probe(lambda k, l: True)(lease).outcome is Outcome.EXECUTED
    assert idempotency_probe(lambda k, l: None)(lease).outcome is Outcome.UNKNOWN
    assert idempotency_probe(lambda k, l: False)(lease).outcome is Outcome.UNKNOWN                    # eventually consistent: not proof
    assert idempotency_probe(lambda k, l: False, authoritative_absence=True)(lease).outcome is Outcome.NOT_EXECUTED
    assert idempotency_probe(lambda k, l: True)({"meta": {}}).outcome is Outcome.UNKNOWN              # no key recorded: cannot look up


def test_one_bad_record_does_not_stop_the_rest(ledger):
    g = guard_with(Grant("pay.send"), ledger=ledger)
    for lid in ("A", "B"):
        make_lease(g, lid)
        g.dispatch_once(lid)

    def flaky(lease):
        if lease["lease_id"] == "A":
            raise KeyError("boom")
        return Finding(Outcome.EXECUTED, provider_ref="p")
    r, _ = recon(g, {"pay.*": flaky})
    rep = r.run_once()
    assert rep.settled == ["B"] and rep.kept == ["A"]


def test_an_observer_that_raises_cannot_change_a_reconciliation(ledger):
    g = guard_with(Grant("pay.send"), ledger=ledger)
    make_lease(g, "L1")
    g.dispatch_once("L1")
    r = Reconciler(g, {"pay.*": lambda l: Finding(Outcome.EXECUTED)}, clock=lambda: NOW, on_event=lambda e: 1 / 0)
    assert r.run_once().settled == ["L1"]


def test_reconciliation_after_a_restart_uses_the_durable_record(tmp_path):
    db = str(tmp_path / "auth.db")
    g1 = guard_with(Grant("pay.send"), ledger=SqliteLedger(db + ".usage"), store=SqliteEnvelopeStore(db))
    make_lease(g1, "L1")
    g1.dispatch_once("L1"); g1.unknown("L1")
    del g1                                                                                      # the process dies
    g2 = AuthorityGuard(SqliteEnvelopeStore(db), SqliteLedger(db + ".usage"), clock=lambda: NOW)
    assert [(d["lease_id"], d["state"]) for d in g2.outstanding(detail=True)] == [("L1", "unknown")]
    r, _ = recon(g2, {"pay.*": idempotency_probe(lambda key, lease: key == "idem-L1")})
    assert r.run_once().settled == ["L1"] and g2.lease("L1")["state"] == "settled"


# ── the runtime never sends a dispatched reservation again ───────────────────────────────
class _P:
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=[IntentStep(outcome="done", need="do it")])


def standing_runtime(handler, ledger=None):
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [CapabilitySpec("op.act", "op", provides=["done"], side_effecting=True, undo="op.undo")]))
    client = InMemoryOperatorClient({"op.act": handler})
    store = MemoryEnvelopeStore()
    store.put(AuthorityEnvelope(tenant="acme", principal="o", mission_id="env", issued_at=NOW - 10, expires_at=NOW + 10_000,
                                grants=(Grant("op.act"),), budget=AutonomyBudget(usd=100, max_actions=100)))
    guard = AuthorityGuard(store, ledger or MemoryLedger(), clock=lambda: NOW)
    rt = MissionRuntime(reg, Executor(client), planner=_P(), authority=guard, authority_key=lambda m: "env")
    m = rt.create_mission("act", policy_refs=["*"], tenant="acme", principal="o",
                          policy=MissionPolicy(id="p", version="1", rules=(
                              PolicyRule("w", Effect.REQUIRE_APPROVAL, "side_effecting", approval_mode=STANDING),)))
    return rt, m, guard, client


@pytest.mark.parametrize("profile", ["development", "production"])
def test_a_retry_after_a_timeout_does_not_send_the_effect_again(profile, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "test" if profile == "development" else "production")
    calls = []

    def handler(i):
        calls.append(i)
        raise TimeoutError("timed out after the request was written")
    if profile == "production":
        led = SqliteLedger(str(tmp_path / "u.db"))
        monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "r.db"))
        store = SqliteEnvelopeStore(str(tmp_path / "e.db"))
        store.put(AuthorityEnvelope(tenant="acme", principal="o", mission_id="env", issued_at=NOW - 10, expires_at=NOW + 10_000,
                                    grants=(Grant("op.act"),), budget=AutonomyBudget(usd=100, max_actions=100)))
        g = AuthorityGuard(store, led, clock=lambda: NOW)
        reg = CapabilityRegistry()
        reg.register(CapabilityManifest("op", [CapabilitySpec("op.act", "op", provides=["done"], side_effecting=True, undo="op.undo")]))
        rt = MissionRuntime(reg, Executor(InMemoryOperatorClient({"op.act": handler})), planner=_P(), authority=g,
                            authority_key=lambda m: "env")
        m = rt.create_mission("act", policy_refs=["*"], tenant="acme", principal="o",
                              policy=MissionPolicy(id="p", version="1", rules=(
                                  PolicyRule("w", Effect.REQUIRE_APPROVAL, "side_effecting", approval_mode=STANDING),)))
        guard = g
    else:
        rt, m, guard, _ = standing_runtime(handler)
    assert rt.run(m.id).state == MissionState.FAILED and len(calls) == 1
    lease_id, state = guard.outstanding("env")[0]
    assert state == "unknown"                                                                 # it may have executed
    node = rt._plans[m.id].graph.nodes[0]
    assert rt._execute(m, rt._plans[m.id], rt._world(m.id), node) is False                    # someone retries the node
    assert len(calls) == 1                                                                    # ... and it is NOT sent again
    ev = events(rt, m.id, "RedispatchRefused")
    assert ev and ev[0]["lease_id"] == lease_id and guard.lease(lease_id)["state"] == "unknown"


def test_the_mission_records_who_and_what_for_every_reservation_and_the_providers_reference():
    rt, m, guard, _ = standing_runtime(lambda i: {"sent": True, "provider_object_id": "msg_77"})
    assert rt.run(m.id).state == MissionState.SUCCEEDED
    rec = guard.ledger.lease_get(f"{m.id}:{rt._plans[m.id].graph.nodes[0].id}:a1")
    assert rec["state"] == "settled"
    meta = rec["meta"]
    assert meta["kind"] == "mission" and meta["mission_id"] == m.id and meta["capability"] == "op.act" and meta["attempt"] == 1
    assert meta["operator"] == "op" and meta["tenant"] == "acme" and meta["provider_ref"] == "msg_77"
    assert meta["dispatched_at"] <= meta["settled_at"] and meta["execution_id"] == meta["idempotency_key"] != ""


def test_a_reconciler_finds_a_stuck_mission_reservation_and_resolves_it_from_the_providers_answer():
    def handler(i):
        raise TimeoutError("timeout")
    rt, m, guard, _ = standing_runtime(handler)
    rt.run(m.id)
    (lid, state), = guard.outstanding("env")
    assert state == "unknown"
    seen = []
    r = Reconciler(guard, {"op.*": idempotency_probe(lambda key, lease: (seen.append(key), True)[1])}, clock=lambda: NOW)
    assert r.run_once().settled == [lid] and seen == [guard.lease(lid)["meta"]["idempotency_key"]] and seen[0]
    assert guard.outstanding("env") == []


# ── a human resolves what no probe can ───────────────────────────────────────────────────
from agentic_os.mission.reconcile import main as reconcile_cli, resolve_manually   # noqa: E402


def test_manual_resolution_is_named_reasoned_and_recorded(ledger):
    g = guard_with(Grant("pay.send", max_calls=1), ledger=ledger)
    make_lease(g, "L1")
    g.dispatch_once("L1"); g.unknown("L1")
    with pytest.raises(ValueError):
        resolve_manually(g, "L1", executed=False, by="", reason="checked the bank")
    with pytest.raises(ValueError):
        resolve_manually(g, "L1", executed=False, by="Alex", reason="ok")
    assert g.lease("L1")["state"] == "unknown"                                                # refused attempts changed nothing
    assert resolve_manually(g, "L1", executed=True, by="Alex M", reason="found the payment in the bank statement",
                            provider_ref="pi_55") is True
    rec = g.lease("L1")
    assert rec["state"] == "settled" and rec["meta"]["resolved_by"] == "Alex M" and rec["meta"]["provider_ref"] == "pi_55"
    assert rec["meta"]["manual_outcome"] == "executed" and rec["meta"]["reconcile_status"] == "resolved_manually"
    assert resolve_manually(g, "L1", executed=False, by="Alex M", reason="changed my mind later") is False   # already final


def test_a_reservation_that_never_dispatched_is_not_resolved_manually(ledger):
    g = guard_with(Grant("pay.send"), ledger=ledger)
    make_lease(g, "L1")
    assert resolve_manually(g, "L1", executed=False, by="Alex", reason="it never left the building") is False
    assert g.lease("L1")["state"] == "reserved"


def test_the_cli_lists_runs_and_resolves(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_AUTHORITY_DB", raising=False)
    db = str(tmp_path / "auth.db")
    g = guard_with(Grant("pay.send"), ledger=SqliteLedger(db + ".usage"), store=SqliteEnvelopeStore(db))
    make_lease(g, "L1")
    g.dispatch_once("L1"); g.unknown("L1")
    assert reconcile_cli(["--db", db, "list"]) == 0
    assert '"lease_id": "L1"' in capsys.readouterr().out
    assert reconcile_cli(["--db", db, "run"]) == 1                                            # no probe: needs a human
    assert "no independent lookup" in capsys.readouterr().out
    assert reconcile_cli(["--db", db, "resolve", "L1", "--not-executed", "--by", "Alex", "--reason", "provider shows nothing sent"]) == 0
    assert reconcile_cli(["--db", db, "run"]) == 0
    assert reconcile_cli(["--db", db, "resolve", "L1", "--executed", "--by", "A", "--reason", "x"]) == 2   # too short a reason
    assert reconcile_cli(["list"]) == 2 and "--db" in capsys.readouterr().out                    # no database named: an error, not a crash
