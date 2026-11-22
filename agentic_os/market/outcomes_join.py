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

from ..integrations.business.contracts import BusinessObject, Provenance
from .experiment_design import CommercialExperiment, ExperimentEvaluation, evaluate
from .ingestion import METRIC_SOURCES, IngestionRefused, refuse_study_caller, verify_attestation

_STUDY_REF_PREFIX = "synthetic-study:"


@dataclass(frozen=True)
class FirstPartyMetric(BusinessObject):
    """One observed first-party metric value for a segment (control/treatment/"") of an experiment or surface.

    ``attestation`` is stamped by the registered source adapter that produced the metric
    (``register_source_adapter(...).make_metric(...)``, see ``market/ingestion.py``); ``FirstPartyLedger.record``
    verifies it. It is not part of ``digest()`` / ``to_dict()``, so those are unchanged."""
    KIND: ClassVar[str] = "market.first_party_metric"
    source: str = ""                     # one of METRIC_SOURCES
    metric: str = ""
    value: float = 0.0
    segment: str = ""                    # control | treatment | "" (baseline)
    surface: str = ""
    higher_is_better: bool = True
    period_start_ms: int = 0
    period_end_ms: int = 0
    attestation: str = ""

    def business_fields(self) -> dict:
        d = super().business_fields()
        d.pop("attestation", None)
        return d


def _is_study_ref(x: object) -> bool:
    return isinstance(x, str) and x.strip().lower().startswith(_STUDY_REF_PREFIX)


def refuse_non_first_party(m: object, *, check_caller: bool = True) -> None:
    """The gate in front of the first-party ledger. UNCONDITIONAL: no feature flag, independent of
    AGENTIC_OS_SYNTHETIC_RESEARCH (it refuses exactly the same with SR-04 and the SSR package disabled).

    By CONTENT / TYPE: refuses an object that is not exactly a ``FirstPartyMetric`` (subclasses, duck types, study
    and synthetic-response objects), anything carrying ``synthetic=True`` or defined in the synthetic-research
    package, and any metric whose fields or provenance mention a ``synthetic-study:`` reference (any case or
    surrounding whitespace). By SOURCE: the ``source`` must be exactly one of ``METRIC_SOURCES`` (a missing, empty or
    whitespace source, a different case, and every unregistered source are refused: strict allow-list, no
    provider-name heuristics). By ORIGIN: the metric must carry a valid adapter attestation (``ingestion.py``).
    BYPASSABLE by code that runs in the same process: these checks stop accidental and casual misuse. With
    ``check_caller`` (the default for writes) a call that has ANY frame of the synthetic-research package on its stack
    is refused."""
    if check_caller:
        refuse_study_caller()
    cls = type(m)
    if getattr(m, "synthetic", False) or "synthetic_research" in getattr(cls, "__module__", ""):
        raise ValueError("a synthetic / study object can never be recorded in the first-party ledger")
    if cls is not FirstPartyMetric:
        raise TypeError("FirstPartyLedger accepts FirstPartyMetric objects only (no subclasses or duck types)")
    prov = m.prov
    if type(prov) is not Provenance:
        raise TypeError("metric provenance must be a Provenance")
    texts = [m.source, m.metric, m.segment, m.surface, m.attestation, prov.provider, prov.provider_ref,
             *prov.evidence_refs]
    if any(_is_study_ref(t) for t in texts):
        raise ValueError("a metric that references a synthetic study is refused by the first-party ledger")
    if not isinstance(m.source, str) or m.source not in METRIC_SOURCES:
        raise ValueError(f"unregistered first-party metric source {m.source!r}; allowed: {', '.join(METRIC_SOURCES)}"
                         " (a missing or empty source is refused; register an adapter for one of these sources)")
    verify_attestation(m)


class FirstPartyLedger:
    """Observed first-party metrics, keyed by (metric, segment). Last write wins per key (latest observation).

    Every metric is verified when it is recorded AND every time it is read (``get``, ``value``, ``higher_is_better``,
    ``metrics``, iteration, ``measured_metric_names``): a recorded metric that was mutated afterwards (for example
    with ``object.__setattr__``) raises ``IngestionRefused`` instead of being served. The storage is private; it is
    not a security boundary against code in the same process, it stops accidental writes."""

    def __init__(self) -> None:
        self.__entries: Dict[Tuple[str, str], FirstPartyMetric] = {}

    def record(self, m: FirstPartyMetric) -> FirstPartyMetric:
        refuse_non_first_party(m)             # unconditional; may raise ValueError / TypeError / IngestionRefused
        self.__entries[(m.metric, m.segment)] = m
        return m

    @staticmethod
    def _checked(m: FirstPartyMetric) -> FirstPartyMetric:
        try:
            refuse_non_first_party(m, check_caller=False)
        except IngestionRefused:
            raise
        except (ValueError, TypeError) as exc:
            raise IngestionRefused(f"a recorded metric failed integrity verification: {exc}") from None
        return m

    def get(self, metric: str, segment: str) -> Optional[FirstPartyMetric]:
        m = self.__entries.get((metric, segment))
        return None if m is None else self._checked(m)

    def value(self, metric: str, segment: str) -> Optional[float]:
        m = self.get(metric, segment)
        return m.value if m is not None else None

    def higher_is_better(self, metric: str, default: bool = True) -> bool:
        for seg in ("treatment", "control", ""):
            m = self.get(metric, seg)
            if m is not None:
                return m.higher_is_better
        return default

    def metrics(self) -> Tuple[FirstPartyMetric, ...]:
        """Every recorded metric, verified, ordered by (metric, segment)."""
        return tuple(self._checked(m) for _, m in sorted(self.__entries.items()))

    def __iter__(self):
        return iter(self.metrics())

    def __len__(self) -> int:
        return len(self.__entries)

    def measured_metric_names(self) -> Tuple[str, ...]:
        return tuple(sorted({self._checked(m).metric for m in self.__entries.values()}))


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
    measured = ledger.measured_metric_names()
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
