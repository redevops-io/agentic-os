"""Strategy → outcome learning memory (Phase 9) — what have WE tested, and what happened?

The durable asset of the whole plane: a growing body of VERIFIED knowledge about which market strategies
actually work for THIS business. It keeps the plan §17 three beliefs STRICTLY separate and never collapses them:

    B1  a competitor actually uses strategy X          (observation)
    B2  X appears to work in the market                (persistence / cross-competitor convergence — still a proxy)
    B3  X works for US                                 (our own experiment outcomes — the only one that decides)

Applicability is derived ONLY from B3 — our measured outcomes — so market popularity can never masquerade as
proof. When a strategy we have already validated shows up again, ranking can prefer it (act faster); when we
have tested it and it hurt us, that is remembered too. Pure + deterministic; outcomes come from Phase-7
measurement (verdicts), observations from Phase-4 cross-competitor detection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

APPLICABILITY = ("UNTESTED", "SUPPORTED_FOR_OUR_FUNNEL", "CONTRADICTED_FOR_OUR_FUNNEL", "MIXED")
_POSITIVE = {"IMPROVED"}
_NEGATIVE = {"WORSE", "GUARDRAIL_STOP"}


@dataclass(frozen=True)
class StrategyBelief:
    strategy_type: str
    strategy_value: str
    # B1 — observation
    competitor_entities: Tuple[str, ...] = ()
    # B2 — market proxy (NOT proof)
    market_survival_ms: int = 0
    cross_competitor_count: int = 0
    # B3 — our own outcomes (the deciding belief)
    experiments: Tuple[Tuple[str, str], ...] = ()     # (experiment_ref, verdict)
    improved: int = 0
    worse: int = 0
    no_effect: int = 0

    @property
    def applicability(self) -> str:
        """Derived ONLY from B3. Market popularity (B1/B2) never makes a strategy 'supported'."""
        if self.improved and not self.worse:
            return "SUPPORTED_FOR_OUR_FUNNEL"
        if self.worse and not self.improved:
            return "CONTRADICTED_FOR_OUR_FUNNEL"
        if self.improved and self.worse:
            return "MIXED"
        return "UNTESTED"

    @property
    def tested(self) -> bool:
        return bool(self.experiments)


class StrategyMemory:
    """Per-(type,value) memory of the three beliefs. B1/B2 updated from observation; B3 from our experiment
    outcomes. Beliefs are returned separately — the caller never gets a single collapsed 'this works'."""

    def __init__(self) -> None:
        self._b: Dict[Tuple[str, str], StrategyBelief] = {}

    def _get(self, t: str, v: str) -> StrategyBelief:
        return self._b.get((t, v), StrategyBelief(strategy_type=t, strategy_value=v))

    def observe(self, strategy_type: str, strategy_value: str, *, entities: Tuple[str, ...] = (),
                market_survival_ms: int = 0) -> StrategyBelief:
        """Record B1/B2 — a strategy seen in the market (independent entities + how long it has survived). This
        can NEVER move applicability; it only accrues market evidence."""
        cur = self._get(strategy_type, strategy_value)
        ents = tuple(sorted(set(cur.competitor_entities) | set(entities)))
        updated = StrategyBelief(
            strategy_type=strategy_type, strategy_value=strategy_value, competitor_entities=ents,
            market_survival_ms=max(cur.market_survival_ms, market_survival_ms),
            cross_competitor_count=len(ents), experiments=cur.experiments, improved=cur.improved,
            worse=cur.worse, no_effect=cur.no_effect)
        self._b[(strategy_type, strategy_value)] = updated
        return updated

    def record_outcome(self, strategy_type: str, strategy_value: str, experiment_ref: str, verdict: str) -> StrategyBelief:
        """Record B3 — the result of OUR experiment testing this strategy. INCONCLUSIVE is ignored (no signal)."""
        cur = self._get(strategy_type, strategy_value)
        if verdict == "INCONCLUSIVE":
            self._b[(strategy_type, strategy_value)] = cur
            return cur
        updated = StrategyBelief(
            strategy_type=strategy_type, strategy_value=strategy_value,
            competitor_entities=cur.competitor_entities, market_survival_ms=cur.market_survival_ms,
            cross_competitor_count=cur.cross_competitor_count,
            experiments=cur.experiments + ((experiment_ref, verdict),),
            improved=cur.improved + (1 if verdict in _POSITIVE else 0),
            worse=cur.worse + (1 if verdict in _NEGATIVE else 0),
            no_effect=cur.no_effect + (1 if verdict == "NO_EFFECT" else 0))
        self._b[(strategy_type, strategy_value)] = updated
        return updated

    def belief(self, strategy_type: str, strategy_value: str) -> StrategyBelief:
        return self._get(strategy_type, strategy_value)

    def tested(self) -> List[StrategyBelief]:
        """Strategies we have actually run an experiment on — 'what have we tested, and what happened?'"""
        return [b for b in self._b.values() if b.tested]

    def prioritize(self, candidates: List[Tuple[str, str]]) -> List[Tuple[Tuple[str, str], str, int]]:
        """Rank incoming (type,value) candidates by what WE already know (act faster when a validated pattern
        reappears): SUPPORTED first, then UNTESTED (by market convergence), MIXED, CONTRADICTED last."""
        order = {"SUPPORTED_FOR_OUR_FUNNEL": 0, "UNTESTED": 1, "MIXED": 2, "CONTRADICTED_FOR_OUR_FUNNEL": 3}
        rows = [((t, v), self._get(t, v).applicability, self._get(t, v).cross_competitor_count)
                for (t, v) in candidates]
        return sorted(rows, key=lambda r: (order.get(r[1], 1), -r[2]))
