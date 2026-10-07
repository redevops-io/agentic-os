"""One canonical commercial outcome envelope (plan §28, P1C reconciliation).

The audit found three parallel ``Outcome`` envelopes (overlays / growth / learning.rewards). Rather than add a
fourth competing one, ``CommercialOutcome`` is the single commercial-decision result type and it *adapts onto the
base* ``overlays.Outcome`` reward envelope + the existing ``reward_sink()`` — so every commercial capability
(funnel change, campaign, quote, deal intervention, order remediation, collection, renewal) feeds the SAME learning
loop uniformly, without a new learner. ``policy_version`` is preserved so a learned decision is never mistaken for
the static default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

from ..overlays import Outcome as RewardOutcome, RewardSink, reward_sink


@dataclass(frozen=True)
class CommercialOutcome:
    """The result of a commercial decision, measured for learning (§28). ``reward`` is explicit when given,
    otherwise derived from ``economic_value`` — authoritative figures are never silently averaged elsewhere."""
    capability: str                                  # e.g. "acquisition.funnel_optimize"
    subject: str                                     # the account/opportunity/site/funnel the decision acted on
    action: str = ""
    hypothesis: str = ""
    executed_at: str = ""
    verification: str = ""                           # how the outcome was verified
    observed_outcome: str = ""
    horizon: str = ""                                # the measurement window
    economic_value: float = 0.0
    reward: Optional[float] = None
    side_effects: Tuple[str, ...] = ()
    confidence: float = 1.0
    policy_version: str = "static/v0"
    mission_id: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)

    @property
    def effective_reward(self) -> float:
        return float(self.reward) if self.reward is not None else float(self.economic_value)

    def to_reward_outcome(self, *, mission_id: Optional[str] = None,
                          policy_version: Optional[str] = None) -> RewardOutcome:
        """Adapt onto the base reward envelope the runtime already records + the learner consumes."""
        ctx = {"capability": self.capability, "subject": self.subject, "action": self.action,
               "hypothesis": self.hypothesis, "observed_outcome": self.observed_outcome,
               "horizon": self.horizon, "economic_value": self.economic_value,
               "confidence": self.confidence, "side_effects": list(self.side_effects)}
        ctx.update(dict(self.context))
        return RewardOutcome(mission_id=mission_id or self.mission_id, reward=self.effective_reward,
                             policy_version=policy_version or self.policy_version, context=ctx)


def record_commercial_outcome(outcome: CommercialOutcome, *, sink: Optional[RewardSink] = None) -> RewardOutcome:
    """Record a commercial outcome onto the shared reward sink (defaults to the process-wide sink)."""
    reward = outcome.to_reward_outcome()
    (sink or reward_sink()).record(reward)
    return reward


__all__ = ["CommercialOutcome", "record_commercial_outcome"]
