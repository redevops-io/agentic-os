"""Strategy primitives + survival / weak-signal metrics over observation history (Phase 4).

Phase 1 gave an immutable per-surface observation history; Phase 4 reads that history as the signal the plan §6
calls for. Three things:

  * **StrategyPrimitive** — the reusable acquisition tactic behind a raw observation (a pricing frame, an
    urgency device, a guarantee), extracted so we reason about *tactics* not diffs.
  * **Change significance** — classify a MarketChange so a 20% price cut never competes with a CSS tweak for
    attention (COSMETIC … PRICE … AVAILABILITY).
  * **Survival / velocity** — persistence, variant count, change velocity and reversion per surface+dimension,
    and **cross-competitor frequency** (how many INDEPENDENT entities run the same tactic). These are weak
    evidence, honestly: a long-surviving tactic adopted by several competitors is a stronger experiment
    candidate than a one-off tweak — but it is still a hypothesis, never proof it works for us.

Pure + deterministic over the Phase-1 ``ObservationHistory``; no new store.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple

from ..integrations.business.contracts import BusinessObject, now_ms
from .history import MarketChange, MarketObservation, ObservationHistory

PRIMITIVE_TYPES = ("pricing_frame", "anchoring", "discount", "bundling", "trial", "guarantee", "urgency",
                   "social_proof", "comparison", "calculator", "cta", "checkout_friction", "shipping",
                   "upsell", "visual_hook", "positioning")

# the structured_state fields each primitive is read from (observed value → tactic)
_PRIMITIVE_FIELDS = {
    "pricing_frame": "pricing_frame", "urgency": "urgency", "guarantee": "guarantee", "trial": "trial",
    "cta": "cta", "offer": "discount", "bundle": "bundling", "shipping": "shipping", "proof": "social_proof",
    "positioning": "positioning",
}

# change significance, most-material first — a MarketChange is classified by its highest-significance dimension.
SIGNIFICANCE_ORDER = ("PRICE", "AVAILABILITY", "PROMOTION", "OFFER", "CONVERSION_PATH", "PRODUCT",
                      "POSITIONING", "POLICY", "CONTENT", "COSMETIC", "UNKNOWN")
_DIMENSION_SIGNIFICANCE = {
    "price": "PRICE", "listed_price": "PRICE", "effective_price": "PRICE", "unit_price": "PRICE",
    "delivered_price": "PRICE", "stock_state": "AVAILABILITY", "delivery_promise": "AVAILABILITY",
    "discount": "PROMOTION", "coupon": "PROMOTION", "promotion": "PROMOTION",
    "offer": "OFFER", "bundle": "OFFER", "trial": "OFFER", "subscription_terms": "OFFER", "guarantee": "OFFER",
    "cta": "CONVERSION_PATH", "form_fields": "CONVERSION_PATH", "friction": "CONVERSION_PATH",
    "title": "PRODUCT", "product_or_service": "PRODUCT",
    "headline": "POSITIONING", "positioning": "POSITIONING", "pricing_frame": "POSITIONING",
    "urgency": "CONTENT", "proof": "CONTENT",
}


@dataclass(frozen=True)
class StrategyPrimitive(BusinessObject):
    """A reusable acquisition tactic observed on a surface (plan §5). ``observed`` marks a value lifted from the
    observation vs inferred."""
    KIND: ClassVar[str] = "market.strategy_primitive"
    type: str = ""                       # one of PRIMITIVE_TYPES
    value: str = ""
    entity_ref: str = ""
    surface_ref: str = ""
    observed: bool = True


def _state_of(o) -> Dict[str, str]:
    if hasattr(o, "as_state"):
        return o.as_state()
    return {k: str(v) for k, v in (getattr(o, "structured_state", {}) or {}).items()}


def extract_primitives(observation, *, entity_ref: str = "") -> List[StrategyPrimitive]:
    """Lift the acquisition tactics present in one observation's structured state."""
    state = _state_of(observation)
    sref = getattr(observation, "surface_ref", "")
    out: List[StrategyPrimitive] = []
    for ptype, field_name in _PRIMITIVE_FIELDS.items():
        val = state.get(ptype) if ptype in state else state.get(field_name, "")
        if val:
            out.append(StrategyPrimitive(prov=observation.prov, type=ptype, value=str(val),
                                         entity_ref=entity_ref, surface_ref=sref))
    return out


def classify_change(change: MarketChange) -> str:
    """The significance class of a change = its most-material changed dimension (plan §15)."""
    classes = {_DIMENSION_SIGNIFICANCE.get(d.split(".")[-1], "UNKNOWN") for d in change.dimensions_changed}
    for level in SIGNIFICANCE_ORDER:
        if level in classes:
            return level
    return "UNKNOWN"


@dataclass(frozen=True)
class SurvivalMetrics:
    surface_ref: str
    dimension: str
    current_survival_ms: int             # how long the current value has held (plan §6 *_survival)
    variant_count: int                   # distinct values seen (plan §6 variant_count)
    change_count: int
    change_velocity_per_day: float       # plan §6 *_velocity
    median_persistence_ms: int           # typical hold time of a change
    reverted_count: int                  # changes later undone


def survival_metrics(history: ObservationHistory, surface_ref: str, dimension: str, *,
                     kind: str = MarketObservation.KIND, now: Optional[int] = None) -> Optional[SurvivalMetrics]:
    """Persistence / variant / velocity / reversion for one surface+dimension over its observation history."""
    series = history.history(surface_ref, kind=kind)
    if not series:
        return None
    now = now if now is not None else now_ms()
    states = [_state_of(o) for o in series]
    values = [s.get(dimension, "") for s in states]
    variant_count = len({v for v in values if v != ""})

    changes = [c for c in history.changes(surface_ref, kind=kind, now=now) if dimension in c.dimensions_changed]
    span_ms = max(1, series[-1].prov.observed_at - series[0].prov.observed_at)
    velocity = len(changes) / (span_ms / 86_400_000.0) if span_ms else 0.0
    last_change_at = max((c.prov.observed_at for c in changes), default=series[0].prov.observed_at)
    median_persist = int(statistics.median([c.persisted_for_ms for c in changes])) if changes else 0
    return SurvivalMetrics(
        surface_ref=surface_ref, dimension=dimension, current_survival_ms=max(0, now - last_change_at),
        variant_count=variant_count, change_count=len(changes),
        change_velocity_per_day=round(velocity, 4), median_persistence_ms=median_persist,
        reverted_count=sum(1 for c in changes if c.reverted))


@dataclass(frozen=True)
class CrossCompetitorStrategy:
    type: str
    value: str
    entity_refs: Tuple[str, ...]
    independent_entity_count: int        # plan §6 cross_competitor_pattern_frequency


def cross_competitor_strategies(primitives_by_entity: Mapping[str, Sequence[StrategyPrimitive]], *,
                                min_entities: int = 2) -> List[CrossCompetitorStrategy]:
    """How many INDEPENDENT entities run each (tactic, value) — the cross-competitor convergence signal. Counts
    distinct entities (not observations), so one entity repeating a tactic never inflates it."""
    by_key: Dict[Tuple[str, str], set] = {}
    for entity_ref, prims in primitives_by_entity.items():
        for p in prims:
            by_key.setdefault((p.type, p.value), set()).add(entity_ref or p.entity_ref)
    out = [CrossCompetitorStrategy(type=t, value=v, entity_refs=tuple(sorted(ents)),
                                   independent_entity_count=len(ents))
           for (t, v), ents in by_key.items() if len(ents) >= min_entities]
    return sorted(out, key=lambda c: c.independent_entity_count, reverse=True)
