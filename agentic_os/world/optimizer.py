"""Cross-channel GTM optimizer — one objective across every acquisition method (GTM doc §44, sponsorship §19).

For the next marginal GTM dollar (and hour of founder attention), which action creates the most expected
qualified-pilot value: direct email, a podcast/YouTube sponsorship, founder LinkedIn, technical content,
retargeting, or wait for more evidence? Every channel becomes a :class:`CandidateAction` with the same
economics, so a $1,500 niche podcast competes directly with $1,500 of ads or 10 hours of founder outreach.
This makes paid media and sponsorships part of the same cost-based thesis as Context Runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class CandidateAction:
    channel: str
    cost_usd: float
    founder_minutes: float
    expected_qualified_reach: float
    expected_pilot_probability: float
    expected_pilot_value: float
    confidence: float = 0.5
    risk: float = 0.1
    ref: str = ""

    def score(self, *, founder_minute_value: float) -> float:
        """Expected pilot value per total cost (dollars + priced founder time), discounted by confidence."""
        total_cost = self.cost_usd + self.founder_minutes / 60.0 * founder_minute_value * 60.0
        ev = self.expected_qualified_reach * self.expected_pilot_probability * self.expected_pilot_value
        return round(ev * self.confidence / max(total_cost, 1.0), 4)


@dataclass
class Allocation:
    chosen: List[CandidateAction] = field(default_factory=list)
    spent: float = 0.0
    founder_minutes: float = 0.0
    rejected: List[CandidateAction] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"spent": round(self.spent, 2), "founder_minutes": self.founder_minutes,
                "chosen": [{"channel": a.channel, "cost": a.cost_usd, "ref": a.ref,
                            "score": a.score(founder_minute_value=2.0)} for a in self.chosen],
                "rejected": [a.channel + ("/" + a.ref if a.ref else "") for a in self.rejected]}


class CrossChannelOptimizer:
    """Greedy value-per-cost allocation under budget + founder-time + risk constraints. A knapsack/bandit
    can replace the greedy step once outcome data exists; the contract (CandidateAction) stays the same."""

    def __init__(self, *, budget_usd: float, founder_minutes: float = 600.0, max_risk: float = 0.5,
                 founder_minute_value: float = 2.0) -> None:
        self.budget = budget_usd
        self.founder_capacity = founder_minutes
        self.max_risk = max_risk
        self._fmv = founder_minute_value

    def allocate(self, candidates: List[CandidateAction]) -> Allocation:
        ranked = sorted([c for c in candidates if c.channel != "wait"],
                        key=lambda c: c.score(founder_minute_value=self._fmv), reverse=True)
        alloc = Allocation()
        for c in ranked:
            if c.risk > self.max_risk:
                alloc.rejected.append(c); continue
            if (alloc.spent + c.cost_usd <= self.budget
                    and alloc.founder_minutes + c.founder_minutes <= self.founder_capacity):
                alloc.chosen.append(c)
                alloc.spent += c.cost_usd
                alloc.founder_minutes += c.founder_minutes
            else:
                alloc.rejected.append(c)
        # "wait / gather more evidence" wins by default only if nothing cleared the bar
        if not alloc.chosen:
            wait = next((c for c in candidates if c.channel == "wait"), None)
            if wait is not None:
                alloc.chosen.append(wait)
        return alloc


def direct_email_action(*, opportunity: Dict[str, Any], has_email: bool) -> CandidateAction:
    """A direct-email candidate for one qualified opportunity — cheap $/reach but needs a verified email."""
    fit = opportunity.get("runtime_fit", 0.5)
    return CandidateAction(channel="direct_email", cost_usd=0.02, founder_minutes=3.0 if has_email else 0.0,
                           expected_qualified_reach=1.0 if has_email else 0.0,
                           expected_pilot_probability=0.08 * fit, expected_pilot_value=25000.0,
                           confidence=0.5 if has_email else 0.1, risk=0.15,
                           ref=opportunity.get("account", ""))


def sponsorship_action(*, placement: Dict[str, Any]) -> CandidateAction:
    return CandidateAction(channel="sponsorship", cost_usd=float(placement.get("price", 0)),
                           founder_minutes=15.0,
                           expected_qualified_reach=float(placement.get("expected_qualified_reach", 0)),
                           expected_pilot_probability=0.004, expected_pilot_value=25000.0,
                           confidence=0.4, risk=0.2, ref=placement.get("venue", ""))
