"""One decision for every execution topology, and authority as a production invariant.

Serial nodes and fork branches go through the same gate (DENY -> mandatory approval -> standing grant -> ordinary
approval -> run). In the production profile a runtime cannot start without a durable guard, and every node must be
covered by the owner's envelope: no envelope, no autonomous action."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _fork_support import CountingOperator, build_registry, spec           # noqa: E402

from agentic_os.mission.authority import (                                  # noqa: E402
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, MemoryEnvelopeStore, MemoryLedger, SqliteEnvelopeStore,
    SqliteLedger)
from agentic_os.mission.executor import Executor                            # noqa: E402
from agentic_os.mission.fork_join import BranchSpec, ForkSpec, JoinSpec    # noqa: E402
from agentic_os.mission.policy import PER_ACTION, STANDING, Effect, MissionPolicy, PolicyRule   # noqa: E402
from agentic_os.mission.profile import ProfileError, profile                # noqa: E402
from agentic_os.mission.runtime import MissionRuntime                       # noqa: E402
from agentic_os.mission.store import EventStore                             # noqa: E402
from agentic_os.mission.types import Budget, MissionState                   # noqa: E402

NOW = 9_000_000.0
GRANTS = ["billing:write", "support:write", "books:write", "compliance:write", "w:write"]


def env(*grants, **kw):
    base = dict(tenant="acme", principal="owner", mission_id="env-1", issued_at=NOW - 10, expires_at=NOW + 3600,
                grants=grants, budget=AutonomyBudget(usd=1000, max_actions=1000))
    base.update(kw)
    return AuthorityEnvelope(**base)


def policy(mode=STANDING):
    return MissionPolicy(id="p", version="1", rules=(
        PolicyRule("writes", Effect.REQUIRE_APPROVAL, "side_effecting", reason="ordinary", approval_mode=mode),))


def make(envelope=None, *, guard=None, mode=STANDING, with_policy=True):
    op = CountingOperator()
    store = MemoryEnvelopeStore()
    if envelope is not None:
        store.put(envelope)
    g = guard or AuthorityGuard(store, MemoryLedger(), clock=lambda: NOW + 1)
    rt = MissionRuntime(build_registry(), Executor(op), store=EventStore(), max_concurrency=1,
                        authority=g, authority_key=lambda m: "env-1")
    rt.forks.poll_s = 0.002
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding",
                          budget=Budget(usd=100.0), tenant="acme", principal="owner")
    if with_policy:
        m.policy = policy(mode)
    return rt, op, m, g


def types(rt, mid, prefix=""):
    return [e.type for e in rt.store.for_mission(mid) if e.type.startswith(prefix)]


def fork(rt, m, cap="w.write", n=3, fid="f1"):
    return rt.forks.run_fork(m.id, spec(fid, n=n, cap=cap))


# ── fork branches use the same standing-grant semantics as serial nodes ─────────────────
def test_fork_branches_honour_a_standing_grant():
    rt, op, m, _ = make(env(Grant("w.write")))
    res = fork(rt, m)
    assert res.status == "satisfied" and sum(op.effects.values()) == 3          # all ran unattended
    assert len(types(rt, m.id, "StandingGrantApplied")) == 3 and not types(rt, m.id, "NodeParked")


def test_fork_branches_keep_the_human_gate_for_unmigrated_rules():
    rt, op, m, _ = make(env(Grant("w.write")), mode=PER_ACTION)
    assert fork(rt, m).status == "parked" and not op.effects


def test_fork_mandatory_approval_is_never_waived():
    rt, op, m, _ = make(env(Grant("w.gated", max_tier=3)))
    res = fork(rt, m, cap="w.gated", n=1)
    assert res.status == "parked" and not op.effects
    assert "mandatory_capability" in [e.payload for e in rt.store.for_mission(m.id) if e.type == "NodeParked"][-1]["rules"]


def test_fork_without_a_matching_grant_parks_with_an_amendment():
    rt, op, m, _ = make(env(Grant("other.thing")))
    assert fork(rt, m, n=1).status == "parked"
    d = [e.payload for e in rt.store.for_mission(m.id) if e.type == "StandingGrantDeclined"][-1]
    assert d["rule"] == "out_of_scope" and d["amendment"] == {"add_grant": {"capability": "w.write"}}


def test_fork_limit_shared_with_the_allowance_not_per_branch():
    rt, op, m, g = make(env(Grant("w.write", max_calls=2)))
    res = fork(rt, m, n=3)
    assert sum(op.effects.values()) == 2 and res.status == "parked"             # third branch hit the grant limit
    assert g.ledger.used("env-1")["g0:calls"] == 2


# ── production profile ──────────────────────────────────────────────────────────────────
@pytest.fixture
def prod(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    store = SqliteEnvelopeStore(str(tmp_path / "e.db"))
    ledger = SqliteLedger(str(tmp_path / "u.db"))
    return store, ledger, AuthorityGuard(store, ledger, clock=lambda: NOW + 1)


def prod_rt(guard):
    op = CountingOperator()
    rt = MissionRuntime(build_registry(), Executor(op), store=EventStore(), max_concurrency=1,
                        authority=guard, authority_key=lambda m: "env-1")
    rt.forks.poll_s = 0.002
    m = rt.create_mission("x", policy_refs=GRANTS, template="onboarding", budget=Budget(usd=100.0), tenant="acme",
                          principal="owner")
    return rt, op, m


def test_production_runtime_cannot_start_without_a_durable_guard(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    with pytest.raises(ProfileError):
        MissionRuntime(build_registry(), Executor(CountingOperator()))
    mem = AuthorityGuard(MemoryEnvelopeStore(), MemoryLedger())
    with pytest.raises(ProfileError):
        MissionRuntime(build_registry(), Executor(CountingOperator()), authority=mem)


def test_removing_the_guard_after_start_is_not_authorization(prod):
    store, ledger, guard = prod
    store.put(env(Grant("*x*")))
    rt, op, m = prod_rt(guard)
    rt.authority = None
    with pytest.raises(ProfileError):
        rt.run(m.id)


def test_production_denies_actions_outside_the_envelope_in_serial_and_fork(prod):
    store, _, guard = prod
    store.put(env(Grant("nothing.relevant")))
    rt, op, m = prod_rt(guard)
    m2 = rt.run(m.id)
    assert m2.state == MissionState.FAILED and m2.outcome["blocked"] == "authority_denied" and not op.effects
    rt, op, m = prod_rt(guard)
    res = rt.forks.run_fork(m.id, spec("f1", n=2))
    assert not op.effects and set(rt.forks.state(m.id, "f1").ids("failed")) == {"b0", "b1"}
    fails = [e.payload["reason"] for e in rt.store.for_mission(m.id) if e.type == "BranchFailed"]
    assert fails and all(r == "authority_denied" for r in fails)


def test_production_without_any_envelope_is_a_hard_stop(prod):
    _, _, guard = prod
    rt, op, m = prod_rt(guard)
    assert rt.run(m.id).outcome["blocked"] == "authority_denied" and not op.effects


def test_production_runs_what_the_envelope_covers_and_records_the_evidence(prod):
    store, ledger, guard = prod
    store.put(env(Grant("w.write"), Grant("w.read")))
    rt, op, m = prod_rt(guard)
    res = rt.forks.run_fork(m.id, spec("f1", n=2))
    assert res.status == "satisfied" and sum(op.effects.values()) == 2
    ev = [e.payload for e in rt.store.for_mission(m.id) if e.type == "ScopeAuthorized"]
    assert len(ev) == 2 and ev[0]["envelope_digest"] == store.get("env-1").digest()
    assert ledger.used("env-1")["actions"] == 2


def test_production_limit_reached_parks_for_a_human_instead_of_failing(prod):
    store, _, guard = prod
    store.put(env(Grant("w.write", max_calls=1)))
    rt, op, m = prod_rt(guard)
    res = rt.forks.run_fork(m.id, spec("f1", n=2))
    assert sum(op.effects.values()) == 1 and res.status == "parked"
    parked = [e.payload for e in rt.store.for_mission(m.id) if e.type == "NodeParked"][-1]
    assert "authority_scope" in parked["rules"]


def test_production_revocation_stops_new_dispatch(prod):
    store, _, guard = prod
    store.put(env(Grant("w.write")))
    rt, op, m = prod_rt(guard)
    assert rt.forks.run_fork(m.id, spec("f1", n=1)).status == "satisfied"
    store.revoke("env-1")
    rt.forks.run_fork(m.id, spec("f2", n=1))
    assert sum(op.effects.values()) == 1


def test_development_default_is_unchanged_and_says_so(monkeypatch, caplog):
    import agentic_os.mission.profile as pr
    monkeypatch.delenv("AGENTIC_OS_PROFILE", raising=False)
    monkeypatch.delenv("AGENTIC_OS_REQUIRE_AUTHORITY", raising=False)
    monkeypatch.setattr(pr, "_warned", False)
    with caplog.at_level("WARNING", logger="agentic_os.profile"):
        assert profile() == "development"
        assert profile() == "development"
    assert sum("NOT enforced" in r.message for r in caplog.records) == 1        # once, not silent
    rt = MissionRuntime(build_registry(), Executor(CountingOperator()))         # no guard needed
    assert rt.authority is None


def test_test_profile_needs_no_durability_and_bad_profile_is_an_error(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "test")
    MissionRuntime(build_registry(), Executor(CountingOperator()),
                   authority=AuthorityGuard(MemoryEnvelopeStore(), MemoryLedger()))
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "prod")
    with pytest.raises(ProfileError):
        profile()


def test_legacy_require_authority_flag_means_production(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_PROFILE", raising=False)
    monkeypatch.setenv("AGENTIC_OS_REQUIRE_AUTHORITY", "1")
    assert profile() == "production"


# ── reservation lifecycle through the runtime ───────────────────────────────────────────
from agentic_os.mission.authority import Lease                               # noqa: E402
from agentic_os.mission.fork_join import branch_node_id                      # noqa: E402


def lid(m, bid, fid="f1", attempt=1):
    """The ledger lease id the runtime derives for a fork branch attempt."""
    return f"{m.id}:{branch_node_id(m.id, fid, bid)}:a{attempt}"


def test_success_settles_the_reservation():
    rt, op, m, g = make(env(Grant("w.write")))
    assert fork(rt, m, n=2).status == "satisfied"
    assert [g.ledger.lease_state(lid(m, f"b{i}")) for i in range(2)] == [Lease.SETTLED.value] * 2
    assert g.outstanding("env-1") == [] and g.ledger.used("env-1")["g0:calls"] == 2


def test_operator_error_on_a_side_effecting_call_stays_counted_as_unknown():
    rt, op, m, g = make(env(Grant("w.write", max_calls=3)))
    op.fail_caps = {"w.write"}
    fork(rt, m, n=1)
    state = g.ledger.lease_state(lid(m, "b0"))
    assert state == Lease.UNKNOWN.value                                       # it may have executed: never freed silently
    assert g.outstanding("env-1") == [(lid(m, "b0"), "unknown")]
    assert g.ledger.used("env-1")["g0:calls"] == 1                            # still counts against the limit
    assert types(rt, m.id, "AuthorityLeaseUnknown") == ["AuthorityLeaseUnknown"]   # on the ledger for the operator
    # independent verification says it did NOT execute: capacity returns
    assert g.reconcile(lid(m, "b0"), executed=False) and g.ledger.used("env-1")["g0:calls"] == 0


def test_unknown_blocks_overspend_until_reconciled():
    rt, op, m, g = make(env(Grant("w.write", max_calls=1)))
    op.fail_caps = {"w.write"}
    fork(rt, m, n=1, fid="f1")
    op.fail_caps = set()
    rt2, op2, m2, _ = make(None, guard=g)                                      # another mission/agent on the same allowance
    res = rt2.forks.run_fork(m2.id, spec("f2", n=1))                          # still held by the unknown one
    assert not op2.effects and res.status == "parked"
    g.reconcile(lid(m, "b0", "f1"), executed=False)
    rt3, op3, m3, _ = make(None, guard=g)
    assert rt3.forks.run_fork(m3.id, spec("f3", n=1)).status == "satisfied" and sum(op3.effects.values()) == 1


def test_failure_that_says_nothing_was_applied_returns_the_reservation():
    class NotApplied(RuntimeError):
        not_applied = True
    rt, op, m, g = make(env(Grant("w.write", max_calls=2)))
    orig = op.invoke

    def inv(operator, capability, inputs, key, *, secrets=None):
        if capability == "w.write":
            raise NotApplied("rejected before sending")
        return orig(operator, capability, inputs, key, secrets=secrets)
    op.invoke = inv
    fork(rt, m, n=1)
    assert g.ledger.lease_state(lid(m, "b0")) == Lease.RELEASED.value and g.ledger.used("env-1")["g0:calls"] == 0


def test_failure_before_dispatch_releases():
    rt, op, m, g = make(env(Grant("w.write", max_calls=5)))
    rt._pre_exec_ok = lambda *a, **k: False                                    # admission refuses AFTER the gate reserved
    fork(rt, m, n=2)
    assert not op.effects
    assert [g.ledger.lease_state(lid(m, f"b{i}")) for i in range(2)] == [Lease.RELEASED.value] * 2
    assert g.ledger.used("env-1")["g0:calls"] == 0 and g.outstanding("env-1") == []


def _node(nid):
    from agentic_os.mission.types import Node
    n = Node(capability="w.write", operator="w-op", side_effecting=True)
    n.id = nid
    return n


def _dec():
    return type("D", (), {"required": True, "standing_eligible": True, "risk_fired": False, "risk_waivable": True,
                          "rules": ["mission_policy"], "policy_digest": ""})()


def test_mission_end_releases_reservations_that_never_dispatched():
    rt, op, m, g = make(env(Grant("w.write", max_calls=5)))
    node, dec = _node("n-x"), _dec()
    verdict, res = rt._authority_gate(m, node, dec, rt._world(m.id), inputs={})
    assert verdict == "run" and g.outstanding("env-1") == [(res.lease_id, "reserved")]
    rt._set_state(m, MissionState.CANCELLED)
    assert g.ledger.lease_state(res.lease_id) == Lease.RELEASED.value


def test_mission_end_never_releases_a_dispatched_reservation():
    rt, op, m, g = make(env(Grant("w.write", max_calls=5)))
    op.fail_caps = {"w.write"}
    fork(rt, m, n=1)
    rt._set_state(m, MissionState.CANCELLED)
    assert g.ledger.lease_state(lid(m, "b0")) == Lease.UNKNOWN.value


def test_regating_the_same_attempt_does_not_count_twice():
    rt, op, m, g = make(env(Grant("w.write", max_calls=5)))
    node, dec = _node("n-y"), _dec()
    for _ in range(4):
        assert rt._authority_gate(m, node, dec, rt._world(m.id), inputs={})[0] == "run"
    assert g.ledger.used("env-1")["g0:calls"] == 1


def test_a_retry_attempt_gets_its_own_reservation():
    rt, op, m, g = make(env(Grant("w.write", max_calls=5)))
    node, dec = _node("n-z"), _dec()
    rt._authority_gate(m, node, dec, rt._world(m.id), inputs={}, attempt=1)
    rt._authority_gate(m, node, dec, rt._world(m.id), inputs={}, attempt=2)
    assert g.ledger.used("env-1")["g0:calls"] == 2
