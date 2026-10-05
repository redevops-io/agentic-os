"""First-party outcome join (Phase 7) — OUR metrics decide, not competitor prices.

The whole plane's thesis: competitor behaviour is evidence of what's worth testing; OUR measured outcomes
determine what works for us. Phases 1-6 produced evidence, comparability, relative position, tactics and a
frozen experiment; Phase 7 joins the experiment to FIRST-PARTY metrics (Umami / GSC / marketplace sales / CRM /
billing / ERP) and turns the control-vs-treatment reading into the experiment's verdict via the Phase-6
evaluator — closing the loop evidence → hypothesis → experiment → measured own-outcome.

A metric carries ``higher_is_better`` so guardrails and direction are computed honestly (a rise in refund_rate
is adverse; a rise in conversion is good). Measurement is deterministic; it reads a ledger of observed metrics,
it does not run the experiment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Optional, Tuple

from ..integrations.business.contracts import BusinessObject
from .experiment_design import CommercialExperiment, ExperimentEvaluation, evaluate

METRIC_SOURCES = ("umami", "gsc", "marketplace", "crm", "billing", "erp", "analytics")


@dataclass(frozen=True)
class FirstPartyMetric(BusinessObject):
    """One observed first-party metric value for a segment (control/treatment/"") of an experiment or surface."""
    KIND: ClassVar[str] = "market.first_party_metric"
    source: str = ""                     # one of METRIC_SOURCES
    metric: str = ""
    value: float = 0.0
    segment: str = ""                    # control | treatment | "" (baseline)
    surface: str = ""
    higher_is_better: bool = True
    period_start_ms: int = 0
    period_end_ms: int = 0


class FirstPartyLedger:
    """Observed first-party metrics, keyed by (metric, segment). Last write wins per key (latest observation)."""

    def __init__(self) -> None:
        self._by_key: Dict[Tuple[str, str], FirstPartyMetric] = {}

    def record(self, m: FirstPartyMetric) -> FirstPartyMetric:
        self._by_key[(m.metric, m.segment)] = m
        return m

    def get(self, metric: str, segment: str) -> Optional[FirstPartyMetric]:
        return self._by_key.get((metric, segment))

    def value(self, metric: str, segment: str) -> Optional[float]:
        m = self.get(metric, segment)
        return m.value if m is not None else None

    def higher_is_better(self, metric: str, default: bool = True) -> bool:
        for seg in ("treatment", "control", ""):
            m = self.get(metric, seg)
            if m is not None:
                return m.higher_is_better
        return default


@dataclass(frozen=True)
class MeasuredOutcome:
    evaluation: ExperimentEvaluation
    primary_metric: str
    control_value: Optional[float]
    treatment_value: Optional[float]
    primary_delta: float
    guardrail_values: Dict[str, float] = field(default_factory=dict)
    measured_metrics: Tuple[str, ...] = ()

    @property
    def verdict(self) -> str:
        return self.evaluation.verdict


def measure_experiment(experiment: CommercialExperiment, ledger: FirstPartyLedger, *, elapsed_s: int,
                       missing: str = "INCONCLUSIVE") -> MeasuredOutcome:
    """Join the experiment to first-party metrics and evaluate it. ``primary_delta`` = treatment − control for
    the primary metric (raw; the evaluator applies the frozen expected direction). Guardrail values are signed
    GOODNESS (negative = adverse), derived with each metric's ``higher_is_better`` so a bad move trips the
    guardrail regardless of metric polarity. If the primary metric has no control/treatment reading yet, the
    outcome is inconclusive — never guessed."""
    c = ledger.value(experiment.primary_metric, "control")
    t = ledger.value(experiment.primary_metric, "treatment")
    measured = tuple(sorted({m for (m, _s) in ledger._by_key}))
    if c is None or t is None:
        ev = ExperimentEvaluation(missing, f"no control/treatment reading for {experiment.primary_metric!r}",
                                  within_guardrails=True, met_minimum_observation=False, primary_delta=0.0)
        return MeasuredOutcome(ev, experiment.primary_metric, c, t, 0.0, {}, measured)

    primary_delta = round(t - c, 6)
    guardrail_values: Dict[str, float] = {}
    for gm in experiment.guardrails:
        gc, gt = ledger.value(gm, "control"), ledger.value(gm, "treatment")
        if gc is None or gt is None:
            continue
        hib = ledger.higher_is_better(gm)
        guardrail_values[gm] = round((gt - gc) if hib else (gc - gt), 6)   # signed goodness

    ev = evaluate(experiment, primary_delta=primary_delta, elapsed_s=elapsed_s, guardrail_values=guardrail_values)
    return MeasuredOutcome(ev, experiment.primary_metric, c, t, primary_delta, guardrail_values, measured)


def conversion_delta(ledger: FirstPartyLedger, metric: str = "conversion", *,
                     baseline_segment: str = "control", recent_segment: str = "treatment") -> Optional[float]:
    """Relative change in a first-party metric (recent vs baseline), e.g. to populate the Phase-3 PricingContext
    `conversion_delta` from real behaviour rather than a guess. None when either reading is absent/zero."""
    base = ledger.value(metric, baseline_segment)
    recent = ledger.value(metric, recent_segment)
    if base in (None, 0) or recent is None:
        return None
    return round((recent - base) / base, 6)
