"""Bounded automation (Phase 8, plan §18).

The final autonomy step lets a deployment delegate a NARROW set of low-downside actions to run without per-action
approval — but only within hard bounds. This is deliberately the most conservative module in the plane: the
default is to deny, and automation is allowed only when ALL of the following hold:

- the action's kind is on an explicit allow-list (reads and internal, reversible prep — never pricing, legal,
  contracts or external commitments);
- its side-effect tier is at or below the bound (defaults to bounded/reversible writes);
- it is reversible enough;
- per-deal and global rate caps for the window are not yet exhausted.

It composes with :func:`agentic_os.deal_closing.autonomy.disposition`: automation can only *downgrade* an approval
to an auto-execute, never *upgrade* anything the autonomy level already gated. So a critical action stays gated
even if someone mis-lists it, because its risk tier fails the bound.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Tuple

from ..agent_gateway.contracts import RiskTier
from .autonomy import ActionDisposition, AutonomyLevel, disposition
from .blockers import InterventionKind
from .candidates import CandidateAction

_K = InterventionKind

# The only kinds ever eligible for unattended execution: reads + reversible internal prep. External comms,
# pricing, legal, contracts and signatures are deliberately excluded regardless of bounds.
DEFAULT_ALLOWED: FrozenSet[InterventionKind] = frozenset({
    _K.VERIFY_CLAIM, _K.IDENTIFY_STAKEHOLDER, _K.QUALIFY_DISCOVERY, _K.MAP_DECISION_PROCESS,
    _K.CLARIFY_CRITERIA, _K.PREPARE_MUTUAL_PLAN, _K.ESCALATE,
})


@dataclass(frozen=True)
class AutomationBounds:
    """The envelope within which actions may auto-run. Conservative defaults; a deployment widens deliberately."""
    allowed_kinds: FrozenSet[InterventionKind] = DEFAULT_ALLOWED
    max_risk_tier: RiskTier = RiskTier.BOUNDED_WRITE
    min_reversibility: float = 0.9
    per_deal_cap: int = 1
    global_cap: int = 20


@dataclass(frozen=True)
class AutomationDecision:
    allowed: bool
    reason: str


class AutomationGovernor:
    """Stateful enforcer of the bounds over a window (counts auto-executions per deal + globally). Deny-by-default:
    any failed check returns a REQUEST_APPROVAL-equivalent, and only an explicit pass increments the counters."""

    def __init__(self, bounds: Optional[AutomationBounds] = None) -> None:
        self.bounds = bounds or AutomationBounds()
        self._per_deal: Dict[str, int] = {}
        self._global = 0

    def _check(self, action: CandidateAction) -> AutomationDecision:
        b = self.bounds
        if action.kind is InterventionKind.NO_ACTION:
            return AutomationDecision(False, "no-action is not executed")
        if action.kind not in b.allowed_kinds:
            return AutomationDecision(False, f"{action.kind.value} is not on the automation allow-list")
        if action.authority() > b.max_risk_tier:
            return AutomationDecision(False, f"{action.authority().name} exceeds the max auto tier "
                                             f"{b.max_risk_tier.name}")
        if action.reversibility < b.min_reversibility:
            return AutomationDecision(False, f"reversibility {action.reversibility} below the "
                                             f"{b.min_reversibility} bound")
        if self._global >= b.global_cap:
            return AutomationDecision(False, "global automation cap reached for the window")
        if self._per_deal.get(action.deal_ref, 0) >= b.per_deal_cap:
            return AutomationDecision(False, "per-deal automation cap reached for the window")
        return AutomationDecision(True, f"{action.kind.value} within bounds")

    def authorize(self, action: CandidateAction, *,
                  autonomy: AutonomyLevel = AutonomyLevel.POLICY_AUTHORIZED) -> AutomationDecision:
        """Decide whether this action may auto-run now, and if so consume a slot. Automation applies only at
        POLICY_AUTHORIZED and only where the autonomy disposition was already EXECUTE — it never escalates a
        gated action. A pass increments the window counters; a deny changes no state."""
        if autonomy < AutonomyLevel.POLICY_AUTHORIZED:
            return AutomationDecision(False, "bounded automation applies only at POLICY_AUTHORIZED")
        if disposition(autonomy, action.authority()) is not ActionDisposition.EXECUTE:
            return AutomationDecision(False, "autonomy level does not auto-execute this risk tier")
        decision = self._check(action)
        if decision.allowed:
            self._per_deal[action.deal_ref] = self._per_deal.get(action.deal_ref, 0) + 1
            self._global += 1
        return decision

    def counts(self) -> Tuple[int, Dict[str, int]]:
        return self._global, dict(self._per_deal)


__all__ = ["AutomationBounds", "AutomationDecision", "AutomationGovernor", "DEFAULT_ALLOWED"]
