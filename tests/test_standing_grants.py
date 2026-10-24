"""Standing grants (mission-authority/v1) and MissionPolicy: who may stand in for an approval, and who may not.

Precedence under test: DENY always blocks; a mandatory approval is never waived; an ordinary REQUIRE_APPROVAL rule
explicitly marked `standing_grant_satisfies` is satisfied by an explicit grant within its limits; everything else keeps the
human gate. Rules that were not migrated behave exactly as before."""
from __future__ import annotations

import threading

import pytest

from agentic_os.mission.authority import (
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, MemoryEnvelopeStore, MemoryLedger, SqliteEnvelopeStore,
    SqliteLedger)
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.policy import (
    PER_ACTION, STANDING, Effect, MissionPolicy, NodeContext, PolicyRule, from_constraints)
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import (
    CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, MissionState)

NOW = 5_000_000.0


class _P:
    def plan(self, mission_id, goal, context):
        return ExecutionIntent(mission_id=mission_id, steps=[IntentStep(outcome="done", need="do it")])


def _events(rt, mid, typ):
    return [e["payload"] for e in rt.repo.timeline(mid) if e["type"] == typ]


def _rule(mode=STANDING, match="side_effecting", effect=Effect.REQUIRE_APPROVAL, id="gate"):
    return PolicyRule(id, effect, match, reason="ordinary gate", approval_mode=mode)


def _policy(*rules):
    return MissionPolicy(id="finance", version="3", rules=tuple(rules) or (_rule(),))


def _env(**kw):
    base = dict(tenant="acme", principal="owner", mission_id="m-env", issued_at=NOW - 10, expires_at=NOW + 3600,
                grants=(Grant("op.act"),),
                budget=AutonomyBudget(usd=100, tokens=0, wall_clock_s=0, max_actions=100))
    base.update(kw)
    return AuthorityEnvelope(**base)


def _rt(env=None, *, spec=None, store=None, ledger=None, now=NOW):
    spec = dict(side_effecting=True, undo="op.undo") if spec is None else spec
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [CapabilitySpec("op.act", "op", provides=["done"], **spec)]))
    rt = MissionRuntime(reg, Executor(InMemoryOperatorClient({"op.act": lambda i: {"ok": True}})), planner=_P())
    st = store or MemoryEnvelopeStore()
    if env is not None:
        st.put(env)
    rt.authority = AuthorityGuard(st, ledger or MemoryLedger(), clock=lambda: now)
    rt.authority_key = lambda m: "m-env"
    return rt, st


def _go(rt, policy=None, **kw):
    m = rt.create_mission("act", policy_refs=["*"], policy=policy or _policy(), tenant="acme", principal="owner", **kw)
    return rt.run(m.id)


# ── the rule field ──────────────────────────────────────────────────────────────────────
def test_existing_policy_digests_are_unchanged_by_the_new_field():
    pol = MissionPolicy(id="finance-prod", version="12", scope="organization", support_message="x", grants=("read:market",),
                        rules=(PolicyRule("a", Effect.REQUIRE_APPROVAL, "side_effecting", reason="r"),
                               PolicyRule("b", Effect.DENY, "irreversible"),
                               PolicyRule("c", Effect.REQUIRE_APPROVAL, "*", max_usd=5.0)))
    assert pol.digest() == "sha256:d40e9de58f01bd7b"                       # recorded before the field existed
    assert from_constraints(["approval:side_effecting"]).digest() == "sha256:6c78fdb062c68f1d"
    assert MissionPolicy.from_dict(pol.to_dict()).digest() == pol.digest()


def test_migrated_rule_changes_the_digest_and_roundtrips():
    a, b = _policy(_rule(PER_ACTION)), _policy(_rule(STANDING))
    assert a.digest() != b.digest()
    assert MissionPolicy.from_dict(b.to_dict()).rules[0].approval_mode == STANDING


def test_invalid_modes_rejected():
    with pytest.raises(ValueError):
        PolicyRule("x", Effect.REQUIRE_APPROVAL, "*", approval_mode="whenever")
    with pytest.raises(ValueError):
        PolicyRule("x", Effect.DENY, "*", approval_mode=STANDING)


def test_outcome_exposes_mode():
    o = _policy(_rule(STANDING)).evaluate(NodeContext(capability="op.act", side_effecting=True))
    assert o.gated and o.approval_mode == STANDING


# ── the runtime precedence table ────────────────────────────────────────────────────────
def test_standing_grant_satisfies_an_ordinary_migrated_gate():
    rt, _ = _rt(_env())
    m = _go(rt)
    assert m.state == MissionState.SUCCEEDED                               # ran unattended
    ev = _events(rt, m.id, "StandingGrantApplied")
    assert ev and ev[0]["envelope_digest"] == _env().digest() and ev[0]["policy_digest"]
    assert not _events(rt, m.id, "NodeParked")


def test_unmigrated_rule_keeps_the_human_gate_even_with_a_matching_grant():
    rt, _ = _rt(_env())
    m = _go(rt, _policy(_rule(PER_ACTION)))
    assert m.state == MissionState.WAITING_HUMAN and not _events(rt, m.id, "StandingGrantApplied")


def test_legacy_approval_constraints_are_never_waived():
    rt, _ = _rt(_env())
    m = rt.create_mission("act", policy_refs=["*"], constraints=["approval:side_effecting"], tenant="acme", principal="owner")
    assert rt.run(m.id).state == MissionState.WAITING_HUMAN


def test_deny_beats_a_standing_grant():
    rt, _ = _rt(_env(), spec=dict(side_effecting=True))                     # irreversible
    pol = _policy(_rule(STANDING), PolicyRule("no_irreversible", Effect.DENY, "irreversible", reason="never"))
    m = _go(rt, pol)
    assert m.state == MissionState.FAILED and m.outcome.get("blocked") == "policy_denied"
    assert not _events(rt, m.id, "StandingGrantApplied")


def test_mandatory_capability_approval_is_never_waived():
    rt, _ = _rt(_env(), spec=dict(side_effecting=True, undo="op.undo", approval_required=True))
    m = _go(rt)
    assert m.state == MissionState.WAITING_HUMAN
    assert "mandatory_capability" in _events(rt, m.id, "NodeParked")[-1]["rules"]
    assert not _events(rt, m.id, "StandingGrantApplied")


def test_no_matching_grant_parks_with_a_proposed_amendment():
    rt, _ = _rt(_env(grants=(Grant("other.thing"),)))
    m = _go(rt)
    assert m.state == MissionState.WAITING_HUMAN
    d = _events(rt, m.id, "StandingGrantDeclined")[-1]
    assert d["rule"] == "out_of_scope" and d["amendment"] == {"add_grant": {"capability": "op.act"}}


def test_missing_envelope_expired_and_revoked_never_fall_back_to_permissive():
    rt, _ = _rt(None)
    assert _go(rt).state == MissionState.WAITING_HUMAN                      # no envelope
    rt, _ = _rt(_env(expires_at=NOW))
    assert _go(rt).state == MissionState.WAITING_HUMAN                      # expired
    rt, st = _rt(_env())
    st.revoke("m-env")
    m = _go(rt)
    assert m.state == MissionState.WAITING_HUMAN and _events(rt, m.id, "StandingGrantDeclined")[-1]["rule"] == "revoked"
    rt, st = _rt(_env())
    st.revoke_all("acme")
    assert _go(rt).state == MissionState.WAITING_HUMAN


def test_wrong_tenant_envelope_is_not_usable():
    rt, _ = _rt(_env(tenant="evil"))
    assert _go(rt).state == MissionState.WAITING_HUMAN


def test_bare_wildcard_grant_cannot_stand_in_for_an_approval():
    rt, _ = _rt(_env(grants=(Grant("*"),)))
    m = _go(rt)
    assert m.state == MissionState.WAITING_HUMAN
    assert _events(rt, m.id, "StandingGrantDeclined")[-1]["rule"] == "grant_not_explicit"


def test_limits_escalate_to_a_human_not_a_silent_run():
    rt, _ = _rt(_env(grants=(Grant("op.act", max_calls=1),)))
    assert _go(rt).state == MissionState.SUCCEEDED
    m2 = _go(rt)
    assert m2.state == MissionState.WAITING_HUMAN
    assert _events(rt, m2.id, "StandingGrantDeclined")[-1]["rule"] == "grant_limit"


def test_usd_budget_is_enforced_per_envelope_across_missions():
    spec = dict(side_effecting=True, undo="op.undo")
    rt, _ = _rt(_env(budget=AutonomyBudget(usd=0.0, max_actions=1)), spec=spec)
    assert _go(rt).state == MissionState.SUCCEEDED
    assert _go(rt).state == MissionState.WAITING_HUMAN                       # max_actions=1 reached


def test_parameters_are_checked_against_the_exact_effective_action():
    env = _env(grants=(Grant("op.act", resources=(("recipient", ("*@customer.com",)),)),))
    rt, _ = _rt(env)
    ok = _go(rt, selected_capability="op.act", inputs={"recipient": "a@customer.com"})
    assert ok.state == MissionState.SUCCEEDED
    bad = _go(rt, selected_capability="op.act", inputs={"recipient": "a@evil.com"})
    assert bad.state == MissionState.WAITING_HUMAN
    assert _events(rt, bad.id, "StandingGrantDeclined")[-1]["rule"] == "resource_out_of_scope"
    missing = _go(rt)                                                       # parameter absent: never allowed
    assert missing.state == MissionState.WAITING_HUMAN


# ── dynamic-risk gates ──────────────────────────────────────────────────────────────────
def _risky_rt(env, *, markers=("op",)):
    rt, st = _rt(env)
    rt.policy_plane.risk_threshold = 0.0            # every node trips the score-based gate
    return rt, st


def test_score_based_risk_gate_needs_an_explicit_opt_in():
    rt, _ = _risky_rt(_env())
    m = _go(rt)
    assert m.state == MissionState.WAITING_HUMAN
    assert _events(rt, m.id, "StandingGrantDeclined")[-1]["rule"] == "risk_not_waived"
    rt, _ = _risky_rt(_env(grants=(Grant("op.act", covers_dynamic_risk=True),)))
    assert _go(rt).state == MissionState.SUCCEEDED


def test_regulatory_risk_is_never_waived_even_with_the_opt_in():
    rt, _ = _risky_rt(_env(grants=(Grant("op.act", covers_dynamic_risk=True),)))
    rt.policy_plane.markers = ("op",)               # "op.act" now reads as regulatory
    m = _go(rt)
    assert m.state == MissionState.WAITING_HUMAN
    assert not _events(rt, m.id, "StandingGrantApplied")


# ── concurrency: ten agents cannot spend one allowance ten times ────────────────────────
@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_concurrent_missions_share_one_allowance(kind, tmp_path):
    if kind == "memory":
        store, ledger = MemoryEnvelopeStore(), MemoryLedger()
    else:
        store, ledger = SqliteEnvelopeStore(str(tmp_path / "a.db")), SqliteLedger(str(tmp_path / "u.db"))
    env = _env(grants=(Grant("op.act", max_calls=3),))
    outs = []

    def run():
        rt, _ = _rt(env, store=store, ledger=ledger)
        outs.append(_go(rt).state)

    ts = [threading.Thread(target=run) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert outs.count(MissionState.SUCCEEDED) == 3 and outs.count(MissionState.WAITING_HUMAN) == 7


# ── durable store and production enforcement ────────────────────────────────────────────
def test_sqlite_envelope_store_versions_survive_restart_and_revocation_is_durable(tmp_path):
    p = str(tmp_path / "env.db")
    s1 = SqliteEnvelopeStore(p)
    s1.put(_env())
    s1.put(_env(forbidden=("x",)))
    s2 = SqliteEnvelopeStore(p)                                           # "restart"
    assert s2.get("m-env").forbidden == ("x",)
    assert [v for v, _ in s2.versions("m-env")] == [1, 2]
    assert s2.get("m-env").digest() == _env(forbidden=("x",)).digest()
    assert not s2.is_revoked("m-env")
    s1.revoke("m-env")
    assert s2.is_revoked("m-env")
    assert s2.epoch("acme") == 0 and s1.revoke_all("acme") == 1 and s2.epoch("acme") == 1


def test_guard_durability_flag(tmp_path):
    assert not AuthorityGuard(MemoryEnvelopeStore(), MemoryLedger()).durable
    assert not AuthorityGuard(SqliteEnvelopeStore(str(tmp_path / "e.db")), MemoryLedger()).durable   # both halves must be durable
    assert AuthorityGuard(SqliteEnvelopeStore(str(tmp_path / "e.db")), SqliteLedger(str(tmp_path / "u.db"))).durable


def test_production_requires_durable_authority(monkeypatch, tmp_path):
    from agentic_os.agent_gateway import AgentGateway
    from agentic_os.agent_gateway.registry import CapabilityRegistry as GwRegistry
    from agentic_os.mission.authority import authority_from_env
    monkeypatch.setenv("AGENTIC_OS_REQUIRE_AUTHORITY", "1")
    monkeypatch.delenv("AGENTIC_OS_AUTHORITY_DB", raising=False)
    with pytest.raises(RuntimeError):
        authority_from_env()
    with pytest.raises(RuntimeError):
        AgentGateway(registry=GwRegistry())                                # no authority at all
    with pytest.raises(RuntimeError):
        AgentGateway(registry=GwRegistry(), authority=AuthorityGuard(MemoryEnvelopeStore(), MemoryLedger()))
    monkeypatch.setenv("AGENTIC_OS_AUTHORITY_DB", str(tmp_path / "a.db"))
    g = authority_from_env()
    assert g.durable
    AgentGateway(registry=GwRegistry(), authority=g)                        # allowed
    monkeypatch.delenv("AGENTIC_OS_REQUIRE_AUTHORITY")
    AgentGateway(registry=GwRegistry())                                    # open core default unchanged
