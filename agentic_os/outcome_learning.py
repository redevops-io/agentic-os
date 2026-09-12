"""Outcome learning — close the loop (AGENTIC_APPS_PROACTIVE_INTELLIGENCE_PLAN §20/§21).

The five deterministic kernels validate their MECHANICS offline. The harder, more consequential claim
is the one synthetic feature-benchmarks cannot make: that the stack can **observe outcomes and change
future action selection** — without sacrificing governance, replayability, or explainability. This
module is that learner.

It reads the shared :class:`~agentic_os.priority_engine.OutcomeLog` and produces a ``utility_fn`` that
:func:`~agentic_os.priority_engine.select_action` uses to adjust which action it favours. Three
properties are deliberate and testable:

  * **Governance-preserving** — the learner only produces a *utility number*; the selected action is
    still routed through ``decide`` (risk tier → approval). Learning changes WHICH action is favoured,
    never whether a human gate applies.
  * **Replayable** — the model is a pure function of the log; the same log yields the same adjustments
    and therefore the same selections. No hidden state, no wall-clock.
  * **Explainable** — for any candidate it can state the prior it started from, the observed mean
    reward, how many (attribution-weighted) outcomes back it, and the resulting blend.

The blend is deliberately simple and transparent (count-weighted shrinkage from the prior toward the
observed mean), not a black box — the point here is the LOOP and its guarantees, not a fancy model.
Validated on a controlled outcome environment in :mod:`agentic_os.outcome_learning_backtest`; wiring it
to real production outcomes is the deployment step, not this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple

from agentic_os.priority_engine import InterventionCandidate, OutcomeLog, UtilityFn


@dataclass
class UtilityModel:
    """Learns an expected utility per (source_app, action_kind) from observed outcomes, and blends it
    with the static prior (the base priority) by how much evidence backs it."""
    prior_weight: float = 5.0                                # shrinkage strength: n/(n+prior_weight)
    reward_weights: Mapping[str, float] = field(default_factory=dict)   # scalarise reward_dimensions
    _stats: Dict[Tuple[str, str], Tuple[float, float]] = field(default_factory=dict)  # key → (Σreward, Σattribution)

    def fit(self, log: OutcomeLog) -> "UtilityModel":
        """Recompute the per-key statistics from the whole log. Pure function of the log (replayable)."""
        stats: Dict[Tuple[str, str], Tuple[float, float]] = {}
        for e in log.events:
            key = (e.source_app, e.action_kind)
            r = e.scalar_reward(self.reward_weights)
            wsum, csum = stats.get(key, (0.0, 0.0))
            # weight each outcome by its attribution confidence (weakly-attributed teaches less)
            stats[key] = (wsum + r, csum + max(0.0, min(1.0, e.attribution_confidence)))
        self._stats = stats
        return self

    def observed(self, key: Tuple[str, str]) -> Optional[Tuple[float, float]]:
        """(mean reward, effective count) for a key, or None when there's no history yet."""
        if key not in self._stats:
            return None
        wsum, csum = self._stats[key]
        return (wsum / csum if csum > 0 else 0.0, csum)

    def predict_key(self, key: Tuple[str, str], base: float) -> float:
        """Count-weighted shrinkage of ``base`` toward the observed mean for ``key`` — the single blend
        used everywhere (selection utility AND held-out value prediction), so the two never drift."""
        obs = self.observed(key)
        if obs is None:
            return base                                       # no data → trust the prior entirely
        mean, n = obs
        w = n / (n + self.prior_weight)                       # more evidence → trust the observed mean more
        return (1.0 - w) * base + w * mean

    def utility(self, candidate: InterventionCandidate, base: float) -> float:
        return self.predict_key(candidate.learn_key, base)

    def explain(self, candidate: InterventionCandidate, base: float) -> str:
        obs = self.observed(candidate.learn_key)
        if obs is None:
            return f"no outcome history for {candidate.learn_key} → prior utility {base:.3f}"
        mean, n = obs
        w = n / (n + self.prior_weight)
        return (f"{candidate.learn_key}: blended prior {base:.3f} with observed mean reward {mean:.3f} "
                f"over {n:.1f} attributed outcomes (weight {w:.2f}) → {self.utility(candidate, base):.3f}")

    def as_utility_fn(self) -> UtilityFn:
        return self.utility
