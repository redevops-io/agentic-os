"""Outcome learning — calibrate discovery priors from realized results (Phase 8, plan §22/§23).

Social demand is an UPSTREAM proxy; actual product behaviour is stronger evidence. Once opportunities ship (or
are validated), join their provenance to what actually happened — did people connect the apps, configure the
workflow, keep using it, pay? — and learn which SOURCES, PAIN CLASSES and APP PAIRS predicted real product
value. Those become priors that reweight future OpportunityScores, so the system gets better at DISCOVERING
products rather than just accumulating posts.

Shrunk-mean calibration (sparse evidence pulls toward 0.5, like the growth/market learning loops); the priors
reweight but never gate — a low prior lowers rank, it does not silence a strong new signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Sequence, Tuple

from .score import ProductOpportunity

_RESULTS = ("VALIDATED", "INVALIDATED", "INCONCLUSIVE")


@dataclass(frozen=True)
class ValidationOutcome:
    """The realized result of validating/shipping an opportunity — the first-party signal that supersedes
    social proxy. ``usage`` holds the funnel (viewed/connected/configured/launched/repeated/paid)."""
    opportunity_id: str
    sources: Tuple[str, ...]             # the social sources the original evidence came from
    classification: str
    applications: Tuple[str, ...]
    result: str = "INCONCLUSIVE"         # VALIDATED | INVALIDATED | INCONCLUSIVE
    usage: Mapping[str, int] = field(default_factory=dict)
    verified_business_outcome: bool = False


def result_from_usage(usage: Mapping[str, int], *, verified_business_outcome: bool = False,
                      repeat_threshold: int = 2) -> str:
    """Derive a result from the usage funnel + verified outcome (behaviour > social proxy). Repeated use or a
    verified business outcome → VALIDATED; launched but never repeated → INVALIDATED; otherwise INCONCLUSIVE."""
    if verified_business_outcome or usage.get("paid", 0) > 0 or usage.get("repeated", 0) >= repeat_threshold:
        return "VALIDATED"
    if usage.get("launched", 0) > 0 and usage.get("repeated", 0) == 0:
        return "INVALIDATED"
    return "INCONCLUSIVE"


@dataclass(frozen=True)
class _Rate:
    trials: int
    successes: int
    rate: float                          # shrunk toward 0.5


def _shrunk(successes: int, trials: int, strength: int) -> float:
    return round((successes + strength * 0.5) / (trials + strength), 4)


@dataclass(frozen=True)
class DiscoveryPriors:
    by_source: Dict[str, _Rate]
    by_classification: Dict[str, _Rate]
    by_application: Dict[str, _Rate]
    strength: int

    def _rate(self, table: Dict[str, _Rate], key: str) -> float:
        r = table.get(key)
        return r.rate if r is not None else 0.5      # unknown → neutral

    def multiplier(self, *, classification: str = "", applications: Sequence[str] = ()) -> float:
        """A ~[0.5, 1.5] reweighting factor from the learned class + app priors (0.5 rate → 1.0, i.e. neutral)."""
        rates = [self._rate(self.by_classification, classification)] if classification else []
        rates += [self._rate(self.by_application, a) for a in applications]
        blended = sum(rates) / len(rates) if rates else 0.5
        return round(0.5 + blended, 4)               # rate 0→0.5×, 0.5→1.0×, 1→1.5×

    def source_success(self, source: str) -> float:
        return self._rate(self.by_source, source)


def calibrate(outcomes: Sequence[ValidationOutcome], *, strength: int = 4) -> DiscoveryPriors:
    """Learn validation-success priors by source / classification / application (INCONCLUSIVE outcomes are not
    counted as trials — they carry no signal)."""
    src: Dict[str, List[int]] = {}       # key -> [trials, successes]
    cls: Dict[str, List[int]] = {}
    app: Dict[str, List[int]] = {}

    def _bump(table, key, success):
        t = table.setdefault(key, [0, 0])
        t[0] += 1
        t[1] += 1 if success else 0

    for o in outcomes:
        if o.result == "INCONCLUSIVE":
            continue
        success = o.result == "VALIDATED"
        for s in o.sources:
            _bump(src, s, success)
        if o.classification:
            _bump(cls, o.classification, success)
        for a in o.applications:
            _bump(app, a, success)

    def _rates(table):
        return {k: _Rate(trials=t, successes=s, rate=_shrunk(s, t, strength)) for k, (t, s) in table.items()}

    return DiscoveryPriors(by_source=_rates(src), by_classification=_rates(cls), by_application=_rates(app),
                           strength=strength)


def apply_priors(opportunities: Sequence[ProductOpportunity],
                 priors: DiscoveryPriors) -> List[Tuple[ProductOpportunity, float]]:
    """Reweight opportunities by the learned priors (first-party outcomes supersede social proxy). Returns
    (opportunity, adjusted_score) sorted descending; the multiplier reweights, it never gates."""
    out = [(o, round(o.score.composite * priors.multiplier(classification=o.coverage.classification,
                                                            applications=o.applications), 4))
           for o in opportunities]
    return sorted(out, key=lambda pair: pair[1], reverse=True)
