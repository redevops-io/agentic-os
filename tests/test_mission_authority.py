"""mission-authority/v1: scope, resources, limits, expiry, revocation, attenuation, durable counters."""
from __future__ import annotations

import math
import threading

import pytest

from agentic_os.mission.authority import (
    Action, AuthorityEnvelope, AuthorityError, AuthorityGuard, AutonomyBudget, Grant, MemoryEnvelopeStore,
    MemoryLedger, SqliteLedger, Verdict, evaluate)

NOW = 1_000_000.0


def env(**kw):
    base = dict(tenant="acme", principal="agent", mission_id="m1", issued_at=NOW, expires_at=NOW + 3600,
                budget=AutonomyBudget(usd=10, tokens=1000, wall_clock_s=3600, max_actions=100, max_children=2,
                                      max_depth=2, max_parallel=4),
                grants=(Grant("crm.*"), Grant("invoice.remind", resources=(("recipient", ("*@customer.com",)),),
                                              max_tier=1, max_calls=2, rate_calls=1, rate_window_s=60)))
    base.update(kw)
    return AuthorityEnvelope(**base)


def guard(e=None, ledger=None):
    st = MemoryEnvelopeStore()
    st.put(e or env())
    return AuthorityGuard(st, ledger or MemoryLedger(), clock=lambda: NOW + 1), st


def act(cap, tier=0, **args):
    return Action(cap, args, tier, side_effecting=tier > 0)


def test_allows_within_grant_and_denies_outside():
    g, _ = guard()
    assert g.authorize("m1", "acme", act("crm.read")).verdict is Verdict.ALLOW
    d = g.authorize("m1", "acme", act("billing.refund"))
    assert d.verdict is Verdict.DENY and d.rule == "out_of_scope" and d.amendment == {"add_grant": {"capability": "billing.refund"}}


def test_forbidden_beats_grant():
    g, _ = guard(env(forbidden=("crm.delete*",)))
    assert g.authorize("m1", "acme", act("crm.read")).allowed
    assert g.authorize("m1", "acme", act("crm.delete_all")).rule == "forbidden"


def test_resource_selector_and_missing_argument_fail_closed():
    g, _ = guard()
    assert g.authorize("m1", "acme", act("invoice.remind", 1, recipient="a@customer.com")).allowed
    assert g.authorize("m1", "acme", act("invoice.remind", 1, recipient="a@evil.com")).rule == "resource_out_of_scope"
    assert g.authorize("m1", "acme", act("invoice.remind", 1)).rule == "resource_out_of_scope"   # absent != allowed
    assert g.authorize("m1", "acme", act("invoice.remind", 1, recipient=["a@customer.com", "b@evil.com"])).rule == "resource_out_of_scope"
    assert g.authorize("m1", "acme", act("invoice.remind", 1, recipient=[])).rule == "resource_out_of_scope"


def test_tier_over_grant_escalates_not_denies():
    g, _ = guard()
    d = g.authorize("m1", "acme", act("crm.update", 2))
    assert d.verdict is Verdict.ESCALATE and d.rule == "tier_over_grant" and d.amendment["raise_tier"]["max_tier"] == 2


def test_amount_limits_per_action_and_cumulative():
    e = env(grants=(Grant("pay.send", max_tier=3, max_amount=100, max_total_amount=150),))
    g, _ = guard(e)
    assert g.authorize("m1", "acme", act("pay.send", 2, amount=90)).allowed
    assert g.authorize("m1", "acme", act("pay.send", 2, amount=101)).rule == "amount_over_limit"
    d = g.authorize("m1", "acme", act("pay.send", 2, amount=90))          # 90 + 90 > 150
    assert d.verdict is Verdict.ESCALATE and d.rule == "grant_limit"
    assert g.authorize("m1", "acme", act("pay.send", 2, amount=50)).allowed   # 90 + 50 <= 150: refusal reserved nothing


def test_non_numeric_and_nan_amounts_never_pass():
    e = env(grants=(Grant("pay.send", max_tier=3, max_amount=100, max_total_amount=1000),))
    g, _ = guard(e)
    assert not g.authorize("m1", "acme", act("pay.send", 2, amount="lots")).allowed
    assert not g.authorize("m1", "acme", act("pay.send", 2, amount=math.nan)).allowed
    assert g.authorize("m1", "acme", Action("pay.send", {}, 2, side_effecting=True)).rule == "amount_missing"


def test_call_count_and_rate_limit():
    g, _ = guard()
    a = act("invoice.remind", 1, recipient="a@customer.com")
    assert g.authorize("m1", "acme", a).allowed
    d = g.authorize("m1", "acme", a)                                       # rate: 1 per 60 s
    assert d.verdict is Verdict.ESCALATE and d.rule == "rate_limit"
    g.clock = lambda: NOW + 100
    assert g.authorize("m1", "acme", a).allowed                            # window passed
    g.clock = lambda: NOW + 300
    assert g.authorize("m1", "acme", a).rule == "grant_limit"              # max_calls=2 reached


def test_action_and_usd_budgets():
    g, _ = guard(env(budget=AutonomyBudget(usd=1.0, max_actions=2, wall_clock_s=3600)))
    assert g.authorize("m1", "acme", Action("crm.read", {}, cost_usd=0.6)).allowed
    d = g.authorize("m1", "acme", Action("crm.read", {}, cost_usd=0.6))
    assert d.rule == "usd_budget" and d.verdict is Verdict.ESCALATE
    assert g.authorize("m1", "acme", Action("crm.read", {}, cost_usd=0.1)).allowed
    assert g.authorize("m1", "acme", Action("crm.read", {})).rule == "action_budget"


def test_zero_budget_is_fail_closed():
    g, _ = guard(env(budget=AutonomyBudget(wall_clock_s=3600)))
    assert g.authorize("m1", "acme", act("crm.read")).rule == "action_budget"


def test_expiry_wall_clock_and_unissued():
    g, _ = guard(env(expires_at=NOW))
    assert g.authorize("m1", "acme", act("crm.read")).rule == "expired"
    g, _ = guard(env(budget=AutonomyBudget(usd=1, max_actions=5, wall_clock_s=0.5)))
    assert g.authorize("m1", "acme", act("crm.read")).rule == "wall_clock_exhausted"
    assert evaluate(AuthorityEnvelope("t", "p", "m"), act("crm.read"), now=NOW).rule == "expired"  # default envelope grants nothing


def test_revocation_epoch_and_tenant_isolation():
    g, st = guard()
    assert g.authorize("m1", "acme", act("crm.read")).allowed
    st.revoke_all("acme")
    assert g.authorize("m1", "acme", act("crm.read")).rule == "revoked"
    g2, _ = guard()
    assert g2.authorize("m1", "other-tenant", act("crm.read")).rule == "no_authority"
    assert g2.authorize("missing", "acme", act("crm.read")).rule == "no_authority"


def test_digest_stable_and_roundtrips_and_changes_with_content():
    e = env()
    assert e.digest() == AuthorityEnvelope.from_dict(e.to_dict()).digest()
    assert e.digest() != env(forbidden=("x",)).digest()
    assert e.digest() == env().digest()


def test_decision_carries_envelope_digest():
    g, _ = guard()
    assert g.authorize("m1", "acme", act("crm.read")).envelope_digest == env().digest()


# ── attenuation ─────────────────────────────────────────────────────────────────────────
def child(parent, **kw):
    base = dict(mission_id="c1", principal="worker", grants=(Grant("crm.read"),),
                budget=AutonomyBudget(usd=2, tokens=100, wall_clock_s=600, max_actions=10, max_children=0, max_depth=0,
                                      max_parallel=1), now=NOW)
    base.update(kw)
    return parent.attenuate(**base)


def test_child_is_a_subset():
    p = env(may_delegate=True)
    c = child(p)
    assert c.depth == 1 and c.parent_digest == p.digest() and c.expires_at <= p.expires_at
    g, st = guard(c)
    assert g.authorize("c1", "acme", act("crm.read")).rule == "orphaned"      # a child whose parent is not on record is denied, never trusted alone
    st.put(p)
    assert g.authorize("c1", "acme", act("crm.read")).allowed
    assert g.authorize("c1", "acme", act("crm.update", 1)).rule == "out_of_scope"


@pytest.mark.parametrize("kw,code", [
    (dict(grants=(Grant("billing.refund"),)), "grant_not_covered"),
    (dict(grants=(Grant("crm.read", max_tier=2),)), "grant_not_covered"),
    (dict(grants=(Grant("invoice.remind"),)), "grant_not_covered"),                       # drops the resource restriction
    (dict(grants=(Grant("invoice.remind", resources=(("recipient", ("*@evil.com",)),)),)), "grant_not_covered"),
    (dict(grants=(Grant("invoice.remind", resources=(("recipient", ("*@customer.com",)),), max_calls=3,
                                          rate_calls=1, rate_window_s=60),)), "grant_not_covered"),   # raises a limit
    (dict(budget=AutonomyBudget(usd=11, max_actions=1, wall_clock_s=10)), "budget_exceeds_parent"),
    (dict(budget=AutonomyBudget(usd=1, max_actions=1, wall_clock_s=999999)), "budget_exceeds_parent"),
    (dict(budget=AutonomyBudget(usd=1, max_actions=1, wall_clock_s=10, max_depth=5)), "budget_exceeds_parent"),
])
def test_child_cannot_exceed_parent(kw, code):
    with pytest.raises(AuthorityError) as ei:
        child(env(may_delegate=True), **kw)
    assert ei.value.code == code


def test_child_keeps_restriction_and_limit_when_equal_or_tighter():
    p = env(may_delegate=True)
    c = child(p, grants=(Grant("invoice.remind", resources=(("recipient", ("a@customer.com",)),), max_calls=1,
                                rate_calls=1, rate_window_s=60),))
    assert c.grants[0].max_calls == 1


def test_delegation_requires_permission_and_depth():
    with pytest.raises(AuthorityError) as ei:
        child(env(may_delegate=False))
    assert ei.value.code == "delegation_not_allowed"
    with pytest.raises(AuthorityError) as ei:
        child(env(may_delegate=True, budget=AutonomyBudget(usd=5, max_actions=5, wall_clock_s=3600, max_children=1, max_depth=0)))
    assert ei.value.code == "max_depth"


def test_grandchild_cannot_outgrow_the_chain():
    p = env(may_delegate=True)
    c = child(p, may_delegate=True, budget=AutonomyBudget(usd=2, tokens=100, wall_clock_s=600, max_actions=10,
                                                          max_children=1, max_depth=1, max_parallel=1))
    gc = c.attenuate(mission_id="g1", principal="w2", grants=(Grant("crm.read"),),
                     budget=AutonomyBudget(usd=1, max_actions=1, wall_clock_s=60), now=NOW)
    assert gc.depth == 2
    with pytest.raises(AuthorityError):
        c.attenuate(mission_id="g2", principal="w2", grants=(Grant("crm.update"),),
                    budget=AutonomyBudget(usd=1, max_actions=1, wall_clock_s=60), now=NOW)


def test_child_quota():
    g, _ = guard(env(may_delegate=True, budget=AutonomyBudget(usd=1, max_actions=5, max_children=1, wall_clock_s=3600)))
    assert g.authorize_delegation("m1", "acme")[0].allowed
    d, _ = g.authorize_delegation("m1", "acme")
    assert d.verdict is Verdict.ESCALATE and d.rule == "child_quota"


# ── counters ────────────────────────────────────────────────────────────────────────────
@pytest.fixture(params=["memory", "sqlite"])
def ledger(request, tmp_path):
    return MemoryLedger() if request.param == "memory" else SqliteLedger(str(tmp_path / "usage.db"))


def test_ledger_atomic_under_concurrency(ledger):
    wins = []

    def go():
        wins.append(ledger.reserve("k", {"n": 1.0}, {"n": 5.0}) is None)

    ts = [threading.Thread(target=go) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(wins) == 5 and ledger.used("k")["n"] == 5.0


def test_ledger_refusal_reserves_nothing_and_release(ledger):
    assert ledger.reserve("k", {"a": 1.0, "b": 9.0}, {"a": 5.0, "b": 5.0}) == "b"
    assert ledger.used("k") == {}
    assert ledger.reserve("k", {"a": 3.0}, {"a": 5.0}) is None
    ledger.release("k", {"a": 3.0})
    assert ledger.used("k")["a"] == 0.0


def test_sqlite_ledger_survives_restart(tmp_path):
    p = str(tmp_path / "u.db")
    assert SqliteLedger(p).reserve("k", {"n": 1.0}, {"n": 1.0}, ("r", 1, 3600.0), now=NOW) is None
    again = SqliteLedger(p)
    assert again.reserve("k", {"n": 1.0}, {"n": 1.0}) == "n"
    assert again.reserve("k", {}, {}, ("r", 1, 3600.0), now=NOW + 1) == "r"
    assert again.reserve("k", {}, {}, ("r", 1, 3600.0), now=NOW + 4000) is None
