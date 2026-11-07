"""Authority is enforced the same way in every execution profile.

No profile has an ambient "development allows" default: a side effect runs only when the owner's envelope covers it, or when the
runtime was given an explicit, named, recorded ``AuthorityTestPolicy``. These tests turn the suite-wide default policy off (the
``no_test_policy`` fixture) so the runtime behaves like an unconfigured deployment."""
from __future__ import annotations

import pytest

from agentic_os.mission.authority import (
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, MemoryEnvelopeStore, MemoryLedger, SqliteEnvelopeStore, SqliteLedger)
from agentic_os.mission.authority_policy import AuthorityTestPolicy
from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor
from agentic_os.mission.profile import ProfileError
from agentic_os.mission.receipt import mission_receipt
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import Budget, MissionState

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]
NOW = 9_000_000.0


class Recording:
    """Wraps the demo fleet and records every capability that actually reached an operator."""

    def __init__(self):
        self.reg, self.fleet = build_fleet()
        self.calls: list[str] = []

    def invoke(self, operator, capability, inputs, idempotency_key, **kw):
        self.calls.append(capability)
        return self.fleet.invoke(operator, capability, inputs, idempotency_key, **kw)


def build(*, test_policy=None, guard=None, tenant=""):
    rec = Recording()
    kw = {"test_policy": test_policy} if test_policy is not None else {}
    if guard is not None:
        kw.update(authority=guard, authority_key=lambda m: "env-1")
    rt = MissionRuntime(rec.reg, Executor(rec), store=EventStore(), **kw)
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding", tenant=tenant,
                          budget=Budget(usd=100.0))
    return rt, rec, m


def types(rt, mid, prefix=""):
    return [e.type for e in rt.store.for_mission(mid) if e.type.startswith(prefix)]


def envelope(*grants, **kw):
    base = dict(tenant="acme", principal="owner", mission_id="env-1", issued_at=NOW - 10, expires_at=NOW + 3600,
                grants=grants, budget=AutonomyBudget(usd=1000, max_actions=1000))
    base.update(kw)
    return AuthorityEnvelope(**base)


def guard_with(env=None):
    store = MemoryEnvelopeStore()
    if env is not None:
        store.put(env)
    return AuthorityGuard(store, MemoryLedger(), clock=lambda: NOW + 1), store


# ── the default: no envelope and no explicit policy means no side effect, in the development profile too ──────────────
def test_development_profile_with_no_envelope_and_no_policy_refuses_side_effects(no_test_policy, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    rt, rec, m = build()
    rt.run(m.id)
    assert m.state is MissionState.FAILED
    assert rec.calls == []                                           # nothing reached an operator
    assert m.outcome["blocked"] == "authority_denied" and m.outcome["rule"] == "no_authority"
    declined = [e for e in rt.store.for_mission(m.id) if e.type == "AuthorityDeclined"]
    assert declined and "test policy" in declined[0].payload["reason"]


def test_unset_profile_behaves_like_development(no_test_policy, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_PROFILE", raising=False)
    monkeypatch.delenv("AGENTIC_OS_REQUIRE_AUTHORITY", raising=False)
    rt, rec, m = build()
    rt.run(m.id)
    assert m.state is MissionState.FAILED and rec.calls == []


def test_test_profile_with_no_policy_also_refuses(no_test_policy, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "test")
    rt, rec, m = build()
    rt.run(m.id)
    assert m.state is MissionState.FAILED and rec.calls == []


# ── an explicit test policy is the only way through, and it is named, pinned, recorded and visible ───────────────────
def test_explicit_test_policy_runs_and_is_recorded_in_log_status_and_receipt(no_test_policy):
    pol = AuthorityTestPolicy.allow_all("local-fixture", "synthetic fleet used by this test")
    rt, rec, m = build(test_policy=pol)
    rt.run(m.id)
    assert m.state is MissionState.WAITING_HUMAN and rec.calls                 # ran up to the compliance gate
    active = [e for e in rt.store.for_mission(m.id) if e.type == "TestPolicyActive"]
    assert len(active) == 1 and active[0].payload["name"] == "local-fixture" and active[0].payload["digest"] == pol.digest()
    used = [e for e in rt.store.for_mission(m.id) if e.type == "TestPolicyAuthorized"]
    assert used and all(e.payload["policy"] == "local-fixture" and e.payload["digest"] == pol.digest() for e in used)
    assert {e.payload["capability"] for e in used} <= set(rec.calls)
    assert rt.authority_posture()["test_policy"]["name"] == "local-fixture"
    assert rt.explain(m.id)["authority"]["basis"] == ["test_policy:local-fixture"]
    assert mission_receipt(rt, m.id)["authority"]["basis"] == ["test_policy:local-fixture"]


def test_test_policy_is_limited_to_the_capabilities_it_lists(no_test_policy):
    pol = AuthorityTestPolicy("narrow", "only the billing step", allow=("billing.*",))
    rt, rec, m = build(test_policy=pol)
    rt.run(m.id)
    assert m.state is MissionState.FAILED                                      # the first non-billing side effect is refused
    assert all(c.startswith("billing.") for c in rec.calls)
    assert m.outcome["blocked"] == "authority_denied"


def test_a_test_policy_needs_a_name_and_a_reason():
    with pytest.raises(ValueError):
        AuthorityTestPolicy("", "why")
    with pytest.raises(ValueError):
        AuthorityTestPolicy("n", "  ")


def test_a_test_policy_is_never_honoured_in_production(no_test_policy, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    db = str(tmp_path / "auth.db")
    guard = AuthorityGuard(SqliteEnvelopeStore(db), SqliteLedger(db + ".usage"), clock=lambda: NOW + 1)
    pol = AuthorityTestPolicy.allow_all("sneaky", "must not work in production")
    rec = Recording()
    with pytest.raises(ProfileError):
        MissionRuntime(rec.reg, Executor(rec), store=EventStore(), authority=guard, test_policy=pol)
    # an inherited class-level default is ignored (not an error) in production: the envelope is still required
    monkeypatch.setattr(MissionRuntime, "test_policy", pol)
    rt = MissionRuntime(rec.reg, Executor(rec), store=EventStore(), authority=guard, authority_key=lambda m: "env-1")
    m = rt.create_mission("Onboard", policy_refs=GRANTS, template="onboarding", tenant="acme", budget=Budget(usd=100.0))
    rt.run(m.id)
    assert m.state is MissionState.FAILED and rec.calls == []
    assert rt.authority_posture()["test_policy"] is None


# ── the owner's envelope is enforced in every profile ────────────────────────────────────────────────────────────────
def test_envelope_scope_is_enforced_in_the_development_profile(no_test_policy, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    guard, _ = guard_with(envelope(Grant("billing.*")))                       # covers billing only
    rt, rec, m = build(guard=guard, tenant="acme")
    rt.run(m.id)
    assert m.state is MissionState.FAILED
    assert all(c.startswith("billing.") for c in rec.calls)
    assert m.outcome["blocked"] == "authority_denied" and m.outcome["rule"] in ("out_of_scope", "resource_out_of_scope")


def test_a_revoked_envelope_is_not_overridden_by_a_test_policy(no_test_policy, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    guard, store = guard_with(envelope(Grant("*")))
    store.revoke("env-1")
    pol = AuthorityTestPolicy.allow_all("would-allow", "must not outrank a revocation")
    rt, rec, m = build(guard=guard, tenant="acme", test_policy=pol)
    rt.run(m.id)
    assert m.state is MissionState.FAILED and rec.calls == []
    assert m.outcome["rule"] == "revoked"


def test_a_covering_envelope_runs_without_any_test_policy(no_test_policy, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "development")
    guard, _ = guard_with(envelope(Grant("*")))
    rt, rec, m = build(guard=guard, tenant="acme")
    rt.run(m.id)
    assert m.state is MissionState.WAITING_HUMAN and rec.calls
    assert types(rt, m.id, "ScopeAuthorized") and not types(rt, m.id, "TestPolicy")
    assert rt.explain(m.id)["authority"]["basis"] == ["envelope"]
