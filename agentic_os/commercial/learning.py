"""Commercial learning loop — one outcome path for every capability (plan §28/§10, P10).

Closes the program: funnel changes, campaigns, offers, quotes, deal interventions, order remediation, service
delivery and collections all emit ONE CommercialOutcome through ONE sink, so the existing self-learning plane
(bandits, off-policy eval, policy promotion) learns across them uniformly without conflating domain metrics.
``learn()`` is the single build-and-record call; it preserves policy_version and causal honesty (confidence is
carried, never inflated). Pure wrapper over outcome.record_commercial_outcome / overlays.reward_sink.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from ..overlays import Outcome as RewardOutcome, RewardSink
from .outcome import CommercialOutcome, record_commercial_outcome


def learn(capability: str, subject: str, *, action: str = "", economic_value: float = 0.0,
          reward: Optional[float] = None, observed_outcome: str = "", verification: str = "",
          hypothesis: str = "", horizon: str = "", confidence: float = 1.0, mission_id: str = "",
          policy_version: str = "static/v0", side_effects: Sequence[str] = (),
          context: Optional[Mapping[str, Any]] = None,
          sink: Optional[RewardSink] = None) -> CommercialOutcome:
    """Build a CommercialOutcome for any capability and record it onto the shared reward sink in one call.
    Returns the outcome (its ``to_reward_outcome`` is what the learner consumes)."""
    outcome = CommercialOutcome(
        capability=capability, subject=subject, action=action, hypothesis=hypothesis,
        verification=verification, observed_outcome=observed_outcome, horizon=horizon,
        economic_value=economic_value, reward=reward, side_effects=tuple(side_effects),
        confidence=confidence, policy_version=policy_version, mission_id=mission_id,
        context=dict(context or {}))
    record_commercial_outcome(outcome, sink=sink)
    return outcome


def learn_many(outcomes: Sequence[CommercialOutcome], *, sink: Optional[RewardSink] = None) -> int:
    """Record several already-built CommercialOutcomes onto the shared sink. Returns the count."""
    for o in outcomes:
        record_commercial_outcome(o, sink=sink)
    return len(outcomes)


__all__ = ["learn", "learn_many"]
