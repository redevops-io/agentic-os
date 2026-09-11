"""Wealth Manager: Decision / Assumption-Drift Monitor — a domain PRODUCER for the shared loop
(AGENTIC_APPS_PROACTIVE_INTELLIGENCE_PLAN §14).

The proactive job here is NOT constant trading advice; it is detecting when reality has drifted enough
that a plan's original assumption deserves reconsideration. So the producer's primary candidate is to
FLAG THE ASSUMPTION FOR REVIEW (a reversible recommendation), with an optional governed rebalance
PROPOSAL — never an autonomous trade. Executing a trade is a CRITICAL financial action outside a
producer's candidate set; anything financial stays permissioned and human-approved (§14/§19).

Deterministic drift gate: drift = |current − assumed| / tolerance (≥1 ⇒ material). Within tolerance,
the only option is 'monitor'. The reward makes a needless flag NEGATIVE, so the loop learns to raise
its hand only when an assumption has genuinely moved — the opposite of noisy trading advice.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate, OutcomeEvent


@dataclass(frozen=True)
class PlanAssumption:
    plan: str
    assumption: str                  # e.g. "long-run inflation ~2%"
    assumed_value: float
    current_value: float
    tolerance: float                 # how far the value may drift before the plan should be revisited
    within_policy: bool = True       # is the strategy still within the client's approved policy?


def drift(a: PlanAssumption) -> float:
    return abs(a.current_value - a.assumed_value) / a.tolerance if a.tolerance > 1e-9 else 0.0


def is_material(a: PlanAssumption) -> bool:
    return drift(a) >= 1.0


def _c(plan: str, kind: str, action: str, ev: float, tier: RiskTier, *, urgency: float = 0.3) -> InterventionCandidate:
    return InterventionCandidate(
        source_app="wealth", subject=plan, proposed_action=action, expected_value=ev, confidence=0.7,
        urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.8,
        required_capabilities=(f"wealth.{kind}",), candidate_id=f"wealth:{plan}:{kind}")


def drift_opportunity(a: PlanAssumption) -> DecisionOpportunity:
    """Only a MATERIALLY drifted assumption produces review candidates; otherwise just 'monitor'. The
    primary action is a reversible review flag; a rebalance PROPOSAL is available but parks on approval.
    No candidate ever executes a trade."""
    d = drift(a)
    actions: list = []
    if is_material(a):
        urgency = min(0.8, 0.3 + 0.15 * d)
        actions.append(_c(a.plan, "flag_for_review",
                          f"Flag '{a.assumption}' for review — the assumption has materially drifted",
                          0.7, RiskTier.BOUNDED_WRITE, urgency=urgency))
        # a proposed allocation change is consequential and parks on approval; it is a PROPOSAL, not a trade
        actions.append(_c(a.plan, "rebalance_proposal", "Draft a rebalance proposal for approval", 0.55,
                          RiskTier.CONSEQUENTIAL, urgency=urgency))
    actions.append(_c(a.plan, "monitor", "No change — keep monitoring the assumption", 0.2, RiskTier.READ,
                      urgency=0.1))
    return DecisionOpportunity(
        entity=a.plan, source_app="wealth", candidate_actions=tuple(actions),
        evidence=(f"drift={d:.2f}x tolerance", f"assumed={a.assumed_value}", f"current={a.current_value}",
                  f"within_policy={a.within_policy}"),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL},   # never a CRITICAL (trade) action
        uncertainty=0.3, opportunity_id=f"wealth:{a.plan}")


def wealth_outcome(action: InterventionCandidate, *, assumption_confirmed_stale: bool = False,
                   plan_improved: bool = False, needless_flag: bool = False,
                   delay_hours: float = 0.0) -> OutcomeEvent:
    """Map the result of a review flag / proposal. A confirmed-stale assumption or an improved plan is
    positive; a NEEDLESS flag (the assumption was fine) is negative — so the loop learns to raise its
    hand only for genuine drift, not to nag."""
    dims = {}
    if assumption_confirmed_stale:
        dims["confirmed_stale"] = 1.0
    if plan_improved:
        dims["plan_improved"] = 1.0
    if needless_flag:
        dims["needless_flag"] = -1.0
    weights = {"confirmed_stale": 0.6, "plan_improved": 1.0, "needless_flag": 0.8}
    raw = sum(weights.get(k, 1.0) * v for k, v in dims.items())
    attribution = 0.7 if delay_hours <= 168 else 0.5      # plan outcomes are slow + hard to attribute
    return OutcomeEvent(candidate_id=action.candidate_id, source_app="wealth",
                        action_kind=action.action_kind, observed_reward=raw, reward_dimensions=dims,
                        delay=delay_hours, attribution_confidence=attribution)
