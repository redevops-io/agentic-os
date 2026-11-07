"""Current execution authority, re-evaluated at the moment of each side effect, in addition to any approval.

An approval says a human agreed to the action. It does not say the owner's authority still covers it. Immediately before each
dispatch (serial nodes, concurrent waves and fork branches alike, and for human-approved nodes too) the runtime asks the
authority again; a revoked, expired or narrowed envelope (or a test policy that does not list the capability) refuses the call and
the refusal is recorded with its reason."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from agentic_os.evidence_query.authz import StaticCeiling                    # noqa: E402
from agentic_os.mission.authority import (                                   # noqa: E402
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, MemoryEnvelopeStore, MemoryLedger)
from agentic_os.mission.authority_policy import AuthorityTestPolicy          # noqa: E402
from agentic_os.mission.demo import build_fleet                              # noqa: E402
from agentic_os.mission.executor import Executor                             # noqa: E402
from agentic_os.mission.runtime import MissionRuntime, ResumeNotAuthorized   # noqa: E402
from agentic_os.mission.store import EventStore                              # noqa: E402
from agentic_os.mission.types import Budget, MissionState                    # noqa: E402
from agentic_os.overlays import Principal                                    # noqa: E402

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]
NOW = 9_000_000.0
CONSENT = "compliance.file_consent"


class Clock:
    def __init__(self):
        self.t = NOW + 1

    def __call__(self):
        return self.t


class Recording:
    def __init__(self):
        self.reg, self.fleet = build_fleet()
        self.calls: list[str] = []

    def invoke(self, operator, capability, inputs, idempotency_key, **kw):
        self.calls.append(capability)
        return self.fleet.invoke(operator, capability, inputs, idempotency_key, **kw)


def envelope(*grants, **kw):
    base = dict(tenant="acme", principal="owner", mission_id="env-1", issued_at=NOW - 10, expires_at=NOW + 3600,
                grants=grants or (Grant("*"),), budget=AutonomyBudget(usd=1000, max_actions=1000))
    base.update(kw)
    return AuthorityEnvelope(**base)


def build(*, env=None, rt_cls=MissionRuntime, store=None, **kw):
    rec, clock, es = Recording(), Clock(), MemoryEnvelopeStore()
    es.put(env or envelope())
    guard = AuthorityGuard(es, MemoryLedger(), clock=clock)
    rt = rt_cls(rec.reg, Executor(rec), store=store or EventStore(), authority=guard, authority_key=lambda m: "env-1", **kw)
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding", tenant="acme", budget=Budget(usd=100.0))
    return rt, rec, m, es, clock, guard


def refusals(rt, mid):
    return [e.payload for e in rt.store.for_mission(mid) if e.type == "ExecutionAuthorityRefused"]


def park_at_consent(rt, m):
    rt.run(m.id)
    assert m.state is MissionState.WAITING_HUMAN
    pend = rt.repo.pending_human(m.id)
    assert pend["capability"] == CONSENT
    return pend["node_id"]


# ── a human-approved node still needs current authority ───────────────────────────────────────────────────────────────
def test_approval_does_not_outlive_a_revoked_envelope():
    rt, rec, m, es, _, _ = build()
    nid = park_at_consent(rt, m)
    es.revoke("env-1")
    rt.approve(m.id, nid, "approve")
    assert CONSENT not in rec.calls
    assert m.state is MissionState.FAILED
    r = refusals(rt, m.id)
    assert len(r) == 1 and r[0]["rule"] == "revoked" and r[0]["approved"] is True and r[0]["capability"] == CONSENT
    assert r[0]["phase"] == "dispatch" and r[0]["reason"]
    assert m.outcome["blocked"] == "authority_denied" and m.outcome["rule"] == "revoked"


def test_approval_does_not_outlive_an_expired_envelope():
    rt, rec, m, _, clock, _ = build()
    nid = park_at_consent(rt, m)
    clock.t = NOW + 7200
    rt.approve(m.id, nid, "approve")
    assert CONSENT not in rec.calls and m.state is MissionState.FAILED
    assert refusals(rt, m.id)[0]["rule"] == "expired"


def test_approval_does_not_outlive_a_narrowed_envelope():
    rt, rec, m, es, _, _ = build()
    nid = park_at_consent(rt, m)
    es.put(envelope(Grant("billing.*"), Grant("support.*"), Grant("books.*")))        # a new version without compliance
    rt.approve(m.id, nid, "approve")
    assert CONSENT not in rec.calls and m.state is MissionState.FAILED
    assert refusals(rt, m.id)[0]["rule"] == "out_of_scope"


def test_an_approved_node_under_a_test_policy_still_needs_the_policy_to_list_it(no_test_policy):
    rec = Recording()
    pol = AuthorityTestPolicy("no-compliance", "approval alone must not be enough", allow=("billing.*", "support.*", "books.*"))
    rt = MissionRuntime(rec.reg, Executor(rec), store=EventStore(), test_policy=pol)
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding", budget=Budget(usd=100.0))
    nid = park_at_consent(rt, m)
    rt.approve(m.id, nid, "approve")
    assert CONSENT not in rec.calls and m.state is MissionState.FAILED
    r = refusals(rt, m.id)[0]
    assert r["rule"] == "no_authority" and r["approved"] is True


def test_an_approved_node_with_no_envelope_and_no_policy_is_refused(no_test_policy):
    rec = Recording()
    pol = AuthorityTestPolicy.allow_all("temporary", "lets the plan reach its gate")
    rt = MissionRuntime(rec.reg, Executor(rec), store=EventStore(), test_policy=pol)
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding", budget=Budget(usd=100.0))
    nid = park_at_consent(rt, m)
    rt.test_policy = None                                                  # the policy is withdrawn while the gate is open
    rt.approve(m.id, nid, "approve")
    assert CONSENT not in rec.calls and m.state is MissionState.FAILED


def test_an_approval_can_extend_an_envelope_limit_but_never_override_a_denial():
    # tier over the grant is an ESCALATE: asking a human to extend it is exactly what an approval is
    rt, rec, m, _, _, _ = build(env=envelope(Grant("compliance.*", max_tier=0), Grant("billing.*"), Grant("support.*"), Grant("books.*")))
    nid = park_at_consent(rt, m)
    rt.approve(m.id, nid, "approve")
    assert CONSENT in rec.calls and m.state is MissionState.SUCCEEDED and not refusals(rt, m.id)


# ── authority is read at the moment of dispatch, not cached from the gate ─────────────────────────────────────────────
class RevokeBeforeSupport(MissionRuntime):
    """Between the gate decision and the dispatch (the admission seam runs in between), the owner revokes."""
    revoke = None

    def _pre_exec_ok(self, m, plan, node) -> bool:
        if node.capability == "support.send_onboarding" and self.revoke is not None:
            self.revoke()
        return True


def test_revocation_between_the_gate_and_the_dispatch_stops_the_call():
    rt, rec, m, es, _, guard = build(rt_cls=RevokeBeforeSupport)
    rt.revoke = lambda: es.revoke("env-1")
    rt.run(m.id)
    assert "support.send_onboarding" not in rec.calls and m.state is MissionState.FAILED
    r = refusals(rt, m.id)
    assert len(r) == 1 and r[0]["capability"] == "support.send_onboarding" and r[0]["rule"] == "revoked" and r[0]["approved"] is False
    assert guard.outstanding("env-1") == []                                # the gate-time reservation was released, not leaked


def test_the_concurrent_wave_path_re_checks_too():
    rt, rec, m, es, _, guard = build(rt_cls=RevokeBeforeSupport, max_concurrency=2)
    rt.revoke = lambda: es.revoke("env-1")
    rt.run(m.id)
    assert "support.send_onboarding" not in rec.calls and m.state is MissionState.FAILED
    assert refusals(rt, m.id)[0]["rule"] == "revoked"


def test_fork_branches_re_check_at_dispatch():
    from test_authority_enforcement import env as fenv, fork, make
    rt, op, m, guard = make(fenv(Grant("w.write")), with_policy=False)
    rt._pre_exec_ok = lambda mm, plan, node: (guard.envelopes.revoke("env-1") or True)
    fork(rt, m, n=2)
    assert not op.effects
    failed = [e.payload for e in rt.store.for_mission(m.id) if e.type == "BranchFailed"]
    assert failed and all(p["reason"] == "authority_denied" for p in failed)
    assert refusals(rt, m.id) and refusals(rt, m.id)[0]["rule"] == "revoked"


# ── composes with the replay gate ──────────────────────────────────────────────────────────────────────────────────────
def test_a_replayed_approved_node_needs_both_the_resume_grant_and_current_authority(tmp_path):
    path = str(tmp_path / "events.jsonl")
    rt, rec, m, es, clock, guard = build(store=EventStore(path=path))
    nid = park_at_consent(rt, m)
    rec2 = Recording()
    rt2 = MissionRuntime(rec2.reg, Executor(rec2), store=EventStore(path=path), authority=guard, authority_key=lambda mm: "env-1")
    rt2.rehydrate(m.id)
    with pytest.raises(ResumeNotAuthorized):                               # still read-only after the rebuild
        rt2.approve(m.id, nid, "approve")
    rt2.grant_resume(m.id, principal=Principal("owner"), ceiling=StaticCeiling({"evidence.acme"}), scope="acme")
    es.revoke("env-1")                                                     # resume is allowed; the envelope is not
    rt2.approve(m.id, nid, "approve")
    assert CONSENT not in rec2.calls and rt2._missions[m.id].state is MissionState.FAILED
    assert refusals(rt2, m.id)[0]["rule"] == "revoked"
