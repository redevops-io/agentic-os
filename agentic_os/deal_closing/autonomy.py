"""Unified autonomy levels (Phase 4, plan §12).

The stack already expresses "how much the system may do on its own" in several places —
``governed_assist`` (only READ-tier auto-runs), ``world.models.ExecutionMode``, ``market.execution_governed``,
and the enterprise ``governance.enforcement`` OBSERVE→ENFORCE ladder — but never under one named taxonomy. The
deal-closing plane names it once: five levels from pure observation to bounded policy-authorized execution. The
level a deployment runs at, combined with an action's side-effect risk tier (reusing
:class:`agentic_os.agent_gateway.contracts.RiskTier`), decides what actually happens to each proposed action.

High-impact actions (pricing, legal, discounting, external representations) stay approval-gated at every level
below POLICY_AUTHORIZED, and even there only bounded/reversible writes auto-run — the honest default is that
consequential side effects require a human unless authority was deliberately delegated.
"""
from __future__ import annotations

import enum
from typing import Mapping

from ..agent_gateway.contracts import RiskTier
from .blockers import InterventionKind


class AutonomyLevel(enum.IntEnum):
    """Ordered autonomy, L0→L4 (plan §12)."""
    OBSERVE = 0            # record verified state only; never surface or take an action
    RECOMMEND = 1          # surface recommendations; take no side-effecting action
    PREPARE = 2            # stage reversible artifacts (draft email, draft quote); do not send
    APPROVAL_GATED = 3     # execute after explicit human approval
    POLICY_AUTHORIZED = 4  # execute bounded/reversible actions within policy, no per-action approval


class ActionDisposition(enum.Enum):
    """What the runtime does with a proposed action under a given autonomy level + risk tier."""
    RECORD = "record"                  # don't even surface it (OBSERVE)
    RECOMMEND = "recommend"            # surface it, no side effect
    PREPARE = "prepare"                # produce a reversible artifact, hold it
    REQUEST_APPROVAL = "request_approval"
    EXECUTE = "execute"


# Default side-effect tier per candidate action kind. External comms / financial / legal are consequential or
# critical and therefore gated; reads and internal prep are low-tier.
_KIND_RISK: Mapping[InterventionKind, RiskTier] = {
    InterventionKind.NO_ACTION: RiskTier.READ,
    InterventionKind.VERIFY_CLAIM: RiskTier.READ,
    InterventionKind.RESOLVE_CONFLICT: RiskTier.READ,
    InterventionKind.QUALIFY_DISCOVERY: RiskTier.BOUNDED_WRITE,
    InterventionKind.IDENTIFY_STAKEHOLDER: RiskTier.READ,
    InterventionKind.MAP_DECISION_PROCESS: RiskTier.BOUNDED_WRITE,
    InterventionKind.CLARIFY_CRITERIA: RiskTier.BOUNDED_WRITE,
    InterventionKind.PREPARE_MUTUAL_PLAN: RiskTier.BOUNDED_WRITE,
    InterventionKind.CONFIRM_BUDGET: RiskTier.CONSEQUENTIAL,
    InterventionKind.ENGAGE_ECONOMIC_BUYER: RiskTier.CONSEQUENTIAL,      # external outreach
    InterventionKind.STRENGTHEN_CHAMPION: RiskTier.CONSEQUENTIAL,
    InterventionKind.COMPETITIVE_DIFFERENTIATION: RiskTier.CONSEQUENTIAL,
    InterventionKind.REQUEST_TECHNICAL_VALIDATION: RiskTier.CONSEQUENTIAL,
    InterventionKind.REQUEST_SECURITY_REVIEW: RiskTier.CONSEQUENTIAL,
    InterventionKind.ENGAGE_PROCUREMENT: RiskTier.CONSEQUENTIAL,
    InterventionKind.FOLLOW_UP_QUOTE: RiskTier.CONSEQUENTIAL,
    InterventionKind.SEND_QUOTE: RiskTier.CONSEQUENTIAL,
    InterventionKind.ADVANCE_LEGAL_REVIEW: RiskTier.CRITICAL,
    InterventionKind.AGREE_PRICING: RiskTier.CRITICAL,                   # pricing/discount = critical
    InterventionKind.SEND_CONTRACT: RiskTier.CRITICAL,
    InterventionKind.ADVANCE_SIGNATURE: RiskTier.CRITICAL,
    InterventionKind.ESCALATE: RiskTier.BOUNDED_WRITE,                   # internal escalation
}


def risk_tier_for(kind: InterventionKind) -> RiskTier:
    return _KIND_RISK.get(kind, RiskTier.CONSEQUENTIAL)   # unknown kinds are gated, not auto-run


def disposition(level: AutonomyLevel, risk_tier: RiskTier) -> ActionDisposition:
    """Decide what happens to an action of ``risk_tier`` under autonomy ``level``. Conservative by design:
    nothing above a bounded write ever runs without approval, and only POLICY_AUTHORIZED auto-runs bounded
    writes; APPROVAL_GATED auto-runs only pure reads."""
    if level <= AutonomyLevel.OBSERVE:
        return ActionDisposition.RECORD
    if level == AutonomyLevel.RECOMMEND:
        return ActionDisposition.RECOMMEND
    if level == AutonomyLevel.PREPARE:
        return ActionDisposition.PREPARE if risk_tier <= RiskTier.BOUNDED_WRITE else ActionDisposition.RECOMMEND
    if level == AutonomyLevel.APPROVAL_GATED:
        return ActionDisposition.EXECUTE if risk_tier == RiskTier.READ else ActionDisposition.REQUEST_APPROVAL
    # POLICY_AUTHORIZED
    return ActionDisposition.EXECUTE if risk_tier <= RiskTier.BOUNDED_WRITE else ActionDisposition.REQUEST_APPROVAL


__all__ = ["AutonomyLevel", "ActionDisposition", "risk_tier_for", "disposition"]
