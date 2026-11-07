"""Two small options of the authority gate that deployments with plan-level approval need.

* ``escalation_parks`` (default True): an action that is inside a grant's scope but past a limit or tier (ESCALATE) parks as an ordinary
  human gate, as before. Set it False and the gate refuses instead (``authority_denied``), for a runtime whose human decision is made
  elsewhere (for example one plan approval) and where an envelope that "must be extended" is a refusal.
* A mission stopped by an authority refusal says which side effects it leaves in place (``effects_remaining``) and that no compensation
  was performed: undoing them is a separate, explicitly authorized action (``MissionRuntime.compensate``)."""
from __future__ import annotations

from agentic_os.mission.authority import (
    AuthorityEnvelope, AuthorityGuard, AutonomyBudget, Grant, MemoryEnvelopeStore, MemoryLedger)
from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import Budget, MissionState

NOW = 9_000_000.0
GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]


def build(*grants, parks=True, rt_cls=MissionRuntime):
    store = MemoryEnvelopeStore()
    store.put(AuthorityEnvelope(tenant="acme", principal="owner", mission_id="env-1", issued_at=NOW - 10, expires_at=NOW + 3600,
                                grants=grants, budget=AutonomyBudget(usd=100, max_actions=100)))
    guard = AuthorityGuard(store, MemoryLedger(), clock=lambda: NOW + 1)
    reg, client = build_fleet()
    rt = rt_cls(reg, Executor(client), store=EventStore(), authority=guard, authority_key=lambda m: "env-1")
    rt.escalation_parks = parks
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding", tenant="acme", budget=Budget(usd=100.0))
    return rt, m, store


def test_an_escalation_parks_by_default():
    rt, m, _ = build(Grant("*", max_tier=0))                    # every side effect is past the tier the grant runs unattended
    rt.run(m.id)
    assert m.state is MissionState.WAITING_HUMAN and rt.repo.pending_human(m.id) is not None
    assert [e for e in rt.store.for_mission(m.id) if e.type == "AuthorityDeclined"][0].payload["verdict"] == "escalate"


def test_with_escalation_parks_false_an_escalation_is_a_refusal():
    rt, m, _ = build(Grant("*", max_tier=0), parks=False)
    rt.run(m.id)
    assert m.state is MissionState.FAILED and rt.repo.pending_human(m.id) is None
    assert m.outcome["blocked"] == "authority_denied" and m.outcome["rule"] == "tier_over_grant"


class RevokeBeforeSupport(MissionRuntime):
    revoke = None

    def _pre_exec_ok(self, m, plan, node) -> bool:
        if node.capability == "support.send_onboarding" and self.revoke is not None:
            self.revoke()
        return True


def test_a_refused_mission_lists_the_side_effects_it_leaves_and_performs_no_compensation():
    rt, m, store = build(Grant("*"), rt_cls=RevokeBeforeSupport)
    rt.revoke = lambda: store.revoke("env-1")
    rt.run(m.id)
    assert m.state is MissionState.FAILED
    done = [e.payload["capability"] for e in rt.store.for_mission(m.id) if e.type == "NodeSucceeded"]
    assert done and m.outcome["effects_remaining"] == done and "support.send_onboarding" not in done      # exactly what ran, none of what was refused
    assert "separate" in m.outcome["compensation"] and not [e for e in rt.store.for_mission(m.id) if e.type.startswith("Compensation")]
