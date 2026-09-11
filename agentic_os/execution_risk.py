"""Projects Execution Risk Radar — deterministic detection of emerging execution failures
(AGENTIC_APPS_PROACTIVE_INTELLIGENCE_PLAN §5).

This is the project-management equivalent of the Growth trend kernel: identify **emerging execution
failures before they become visible failures**, and be honest about uncertainty rather than raising a
dashboard alarm. It measures risk signals over a milestone's state, scores them with a transparent
ensemble, calibrates the score against historical outcomes, estimates a slip window, names the primary
cause, and **abstains** when the evidence is thin — it does not guess.

    signals → weighted ensemble raw risk → calibrated confidence → slip-window estimate →
    primary cause → risk level → threshold / abstain → RiskReport

Positioned as *execution intelligence above* the project system, not another task manager. Like the
trend kernel it makes no accuracy claim by itself — :mod:`agentic_os.execution_risk_backtest` decides
whether a high-confidence threshold beats a naive burndown / blocked-count baseline on historical
replay, with useful lead time. Pure Python, no numpy: deterministic and reproducible.

The a-priori weights are NOT fit to the benchmark (that would make the backtest circular). The PAV
calibrator is reused from the trend kernel — it is a general isotonic regressor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from agentic_os.trend_intelligence import IsotonicCalibrator


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ── the milestone's observed state (plan §5 evidence sources) ────────────────────────
@dataclass(frozen=True)
class ProjectSignals:
    """One milestone under assessment. ``progress_series`` is the cumulative fraction complete over
    recent equal periods (oldest→newest, each in [0,1]); the rest are the plan's other risk inputs,
    each normalised so a detector can read it directly. Only ``milestone`` is required."""
    milestone: str
    days_remaining: float = 30.0
    work_remaining: float = 0.5                  # fraction of the milestone still to do, 0..1
    progress_series: Tuple[float, ...] = ()      # cumulative fraction done over recent days, 0..1
    dependency_lags: Tuple[float, ...] = ()      # each critical dependency's lag behind plan, 0..1
    blocked_dependencies: int = 0
    open_critical_decisions: int = 0
    oldest_decision_age_days: float = 0.0
    requirement_ambiguity: float = 0.0           # 0..1
    owner_overload: float = 0.0                  # 0..1 (max across owners on the critical path)
    approval_pending: bool = False
    approval_age_days: float = 0.0
    test_coverage: float = 1.0                   # 0..1 (1 = fully covered)
    integration_unverified: float = 0.0          # 0..1 (unverified share of integration points)
    coordination_gap: float = 0.0                # 0..1 (pending cross-team handoffs / comms gaps)
    external_risk: float = 0.0                   # 0..1 (external dependency risk)


class RiskLevel(Enum):
    NONE = "none"
    EMERGING = "emerging"
    ELEVATED = "elevated"
    CRITICAL = "critical"


# ── helpers ───────────────────────────────────────────────────────────────────────────
def _daily_rate(series: Sequence[float]) -> float:
    """Mean day-over-day progress (fraction/day) over the window; 0 if flat or unknown."""
    if len(series) < 2:
        return 0.0
    diffs = [b - a for a, b in zip(series, series[1:])]
    return max(0.0, sum(diffs) / len(diffs))


def _required_rate(s: ProjectSignals) -> float:
    return s.work_remaining / s.days_remaining if s.days_remaining > 1e-9 else float("inf")


# ── signal detectors (pure; each 0..1, higher = more risk) ───────────────────────────
def schedule_pressure(s: ProjectSignals) -> float:
    """Are we progressing fast enough to finish by the date? Actual rate vs the rate the remaining
    work needs. At/above required → 0; half the required rate → ~0.5; stalled with work left → 1."""
    req = _required_rate(s)
    if req == float("inf"):                      # no time left but work remains ⇒ maximal pressure
        return 1.0 if s.work_remaining > 1e-6 else 0.0
    if req <= 0:
        return 0.0
    return _clamp(1.0 - _daily_rate(s.progress_series) / req)


def progress_deceleration(s: ProjectSignals) -> float:
    """Is progress slowing down (convex-down burn)? Mean negative second difference, normalised."""
    if len(s.progress_series) < 3:
        return 0.0
    diffs = [b - a for a, b in zip(s.progress_series, s.progress_series[1:])]
    accels = [b - a for a, b in zip(diffs, diffs[1:])]
    mean_accel = sum(accels) / len(accels)
    scale = (max(s.progress_series) - min(s.progress_series)) or 1.0
    return _clamp(-mean_accel / scale * 5.0)     # only deceleration (negative accel) is risk


def dependency_lag(s: ProjectSignals) -> float:
    """How far the critical-path dependencies are behind the rate downstream work needs."""
    if not s.dependency_lags:
        return 0.0
    return _clamp(max(s.dependency_lags))        # the worst critical dependency drives the risk


def blocked_dependencies(s: ProjectSignals) -> float:
    return _clamp(s.blocked_dependencies / 3.0)  # 3+ concurrent blocks ⇒ full signal


def decision_debt(s: ProjectSignals) -> float:
    """Unresolved critical decisions, weighted by how long the oldest has been open (age → critical)."""
    if s.open_critical_decisions <= 0:
        return 0.0
    age_factor = _clamp(s.oldest_decision_age_days / 14.0)   # 2 weeks open ⇒ fully aged
    return _clamp(0.4 * _clamp(s.open_critical_decisions / 3.0) + 0.6 * age_factor)


def requirement_ambiguity(s: ProjectSignals) -> float:
    return _clamp(s.requirement_ambiguity)


def owner_overload(s: ProjectSignals) -> float:
    return _clamp(s.owner_overload)


def stale_approval(s: ProjectSignals) -> float:
    if not s.approval_pending:
        return 0.0
    return _clamp(s.approval_age_days / 7.0)     # an approval pending a week ⇒ full signal


def under_testing(s: ProjectSignals) -> float:
    """Under-testing only matters as the milestone nears — weight the gap by how little time is left."""
    gap = _clamp(1.0 - s.test_coverage)
    nearness = _clamp(1.0 - s.days_remaining / 30.0)
    return _clamp(gap * (0.3 + 0.7 * nearness))


def integration_risk(s: ProjectSignals) -> float:
    return _clamp(s.integration_unverified)


def coordination_risk(s: ProjectSignals) -> float:
    return _clamp(s.coordination_gap)


def external_risk(s: ProjectSignals) -> float:
    return _clamp(s.external_risk)


# ── ensemble (plan §5) ────────────────────────────────────────────────────────────────
#: Plausible a-priori weights (NOT fit to any benchmark). Schedule/dependency signals dominate
#: because they most directly cause slips; the rest are contributing risks.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "schedule_pressure": 0.20, "progress_deceleration": 0.12, "dependency_lag": 0.18,
    "blocked_dependencies": 0.10, "decision_debt": 0.09, "requirement_ambiguity": 0.06,
    "owner_overload": 0.06, "stale_approval": 0.05, "test_insufficiency": 0.06,
    "integration_risk": 0.05, "coordination_risk": 0.02, "external_risk": 0.01,
}
_DETECTORS = {
    "schedule_pressure": schedule_pressure, "progress_deceleration": progress_deceleration,
    "dependency_lag": dependency_lag, "blocked_dependencies": blocked_dependencies,
    "decision_debt": decision_debt, "requirement_ambiguity": requirement_ambiguity,
    "owner_overload": owner_overload, "stale_approval": stale_approval,
    "test_insufficiency": under_testing, "integration_risk": integration_risk,
    "coordination_risk": coordination_risk, "external_risk": external_risk,
}
_CAUSE_TEXT = {
    "schedule_pressure": "progress is below the rate the remaining work needs before the milestone date",
    "progress_deceleration": "progress is decelerating relative to earlier in the window",
    "dependency_lag": "a critical-path dependency is progressing below the rate downstream work needs",
    "blocked_dependencies": "one or more dependencies are currently blocked",
    "decision_debt": "an unresolved critical decision has been open long enough to threaten the plan",
    "requirement_ambiguity": "requirements are ambiguous enough to cause rework",
    "owner_overload": "a critical-path owner is overloaded",
    "stale_approval": "a required approval has been pending too long",
    "test_insufficiency": "test coverage is low with little time remaining to recover",
    "integration_risk": "integration points remain unverified",
    "coordination_risk": "cross-team coordination handoffs are pending",
    "external_risk": "an external dependency is at risk",
}


def signal_vector(s: ProjectSignals) -> Dict[str, float]:
    return {name: fn(s) for name, fn in _DETECTORS.items()}


def raw_risk(s: ProjectSignals, weights: Optional[Mapping[str, float]] = None) -> float:
    """Weighted mean of the (all risk-increasing) detectors, normalised to 0..1."""
    w = weights or DEFAULT_WEIGHTS
    sig = signal_vector(s)
    total_w = sum(w.get(k, 0.0) for k in sig) or 1.0
    return _clamp(sum(w.get(k, 0.0) * sig[k] for k in sig) / total_w)


def primary_cause(s: ProjectSignals, weights: Optional[Mapping[str, float]] = None) -> str:
    """The highest weight×signal contributor — what to point at first (plan §5 'Primary cause')."""
    w = weights or DEFAULT_WEIGHTS
    sig = signal_vector(s)
    ranked = max(sig, key=lambda k: w.get(k, 0.0) * sig[k])
    if sig[ranked] <= 1e-9:
        return "no dominant risk signal"
    return _CAUSE_TEXT.get(ranked, ranked)


def slip_window_days(s: ProjectSignals) -> Tuple[float, float]:
    """Estimated slip (low, high) in days: how much later than the date the work would actually land
    at the current rate. A ±20% band around the point estimate; (0, 0) when on track."""
    rate = _daily_rate(s.progress_series)
    if s.work_remaining <= 1e-6:
        return (0.0, 0.0)
    if rate <= 1e-9:                             # no measurable progress — a large, capped slip
        est = max(0.0, s.days_remaining)
    else:
        eta = s.work_remaining / rate
        est = max(0.0, eta - s.days_remaining)
    return (round(est * 0.8, 1), round(est * 1.2, 1))


# ── precision/recall modes + the report (plan §5/§7) ─────────────────────────────────
class Mode(Enum):
    STRICT = "strict"           # 0.90 — flag only high-confidence risks, abstain otherwise
    BALANCED = "balanced"       # 0.75
    EXPLORATORY = "exploratory" # 0.55


_MODE_THRESHOLD = {Mode.STRICT: 0.90, Mode.BALANCED: 0.75, Mode.EXPLORATORY: 0.55}
_MIN_PROGRESS_POINTS = 3        # too few progress observations ⇒ evidence too thin ⇒ abstain


@dataclass(frozen=True)
class RiskReport:
    milestone: str
    raw_score: float
    confidence: float                       # calibrated P(this milestone slips)
    risk_level: RiskLevel
    slip_window_days: Tuple[float, float]
    primary_cause: str
    signals: Mapping[str, float]
    abstained: bool
    reasons: Tuple[str, ...] = ()


def _risk_level(confidence: float, slip_high: float) -> RiskLevel:
    if confidence >= 0.85 and slip_high > 0:
        return RiskLevel.CRITICAL
    if confidence >= 0.65:
        return RiskLevel.ELEVATED
    if confidence >= 0.5:
        return RiskLevel.EMERGING
    return RiskLevel.NONE


def assess(s: ProjectSignals, *, calibrator: Optional[IsotonicCalibrator] = None,
           mode: Mode = Mode.BALANCED, weights: Optional[Mapping[str, float]] = None) -> RiskReport:
    """Score a milestone's execution risk and decide whether to surface it (or abstain). Confidence is
    calibrated when a fitted calibrator is supplied; otherwise it is the raw score (identity)."""
    raw = raw_risk(s, weights)
    conf = calibrator.transform(raw) if (calibrator and calibrator.fitted) else raw
    sig = signal_vector(s)
    window = slip_window_days(s)
    reasons: List[str] = []
    threshold = _MODE_THRESHOLD[mode]
    if len(s.progress_series) < _MIN_PROGRESS_POINTS:
        reasons.append(f"evidence too thin (needs ≥{_MIN_PROGRESS_POINTS} progress observations)")
    if s.work_remaining <= 1e-6:
        reasons.append("milestone already complete")
    if conf < threshold:
        reasons.append(f"risk confidence {conf:.2f} below the {mode.value} threshold {threshold:.2f}")
    return RiskReport(
        milestone=s.milestone, raw_score=raw, confidence=conf,
        risk_level=_risk_level(conf, window[1]), slip_window_days=window,
        primary_cause=primary_cause(s, weights), signals=sig,
        abstained=bool(reasons), reasons=tuple(reasons))
