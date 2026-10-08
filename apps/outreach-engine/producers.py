"""Decision producer for outreach-engine (runtime-native, plan §3.4/§5).

Pipeline/queue state -> candidates over outreach-engine's real capabilities. Sending the approved
batch is the approval-gated write (CONSEQUENTIAL, matching the operator); approving discovered
accounts is a bounded write; refreshing the queue is a read.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

EMITTED_CAPABILITIES = ("outreach.refresh", "outreach.approve", "outreach.send_all")


@dataclass(frozen=True)
class OutreachSignals:
    campaign: str
    stale_queue: bool = False
    pending_approvals: int = 0
    approved_ready: int = 0


def _c(campaign, kind, action, cap, ev, tier, *, conf=0.7, urgency=0.3):
    return InterventionCandidate(
        source_app="outreach-engine", subject=campaign, proposed_action=action, expected_value=ev,
        confidence=conf, urgency=urgency, action_kind=kind, risk_tier=tier, reversibility=0.4,
        required_capabilities=(cap,), candidate_id=f"outreach:{campaign}:{kind}")


def outreach_opportunity(s: OutreachSignals) -> DecisionOpportunity:
    a = []
    if s.stale_queue:
        a.append(_c(s.campaign, "refresh", "Refresh the outreach queue from the CRM",
                    "outreach.refresh", 0.3, RiskTier.READ, urgency=0.3))
    if s.pending_approvals > 0:
        a.append(_c(s.campaign, "approve", "Approve the discovered accounts for outreach",
                    "outreach.approve", 0.45, RiskTier.BOUNDED_WRITE, urgency=0.4))
    if s.approved_ready > 0:
        a.append(_c(s.campaign, "send_all", "Send the approved outreach batch",
                    "outreach.send_all", 0.6, RiskTier.CONSEQUENTIAL, conf=0.8, urgency=0.6))
    return DecisionOpportunity(
        entity=s.campaign, source_app="outreach-engine", candidate_actions=tuple(a),
        evidence=(f"stale_queue={s.stale_queue}", f"pending={s.pending_approvals}",
                  f"approved_ready={s.approved_ready}"),
        constraints={"max_risk_tier": RiskTier.CONSEQUENTIAL}, opportunity_id=f"outreach:{s.campaign}")


__all__ = ["OutreachSignals", "EMITTED_CAPABILITIES", "outreach_opportunity"]
