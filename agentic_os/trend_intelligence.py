"""ReDevOps Trend Intelligence — deterministic early-emergence detection + calibrated confidence.

Thesis (AGENTIC_TREND_INTELLIGENCE_IMPLEMENTATION_PLAN.md §1/§29): don't ask a model to *guess* what
will trend — **measure** emergence across independent sources, score it with a transparent ensemble,
**calibrate** the score against historical outcomes, and **abstain** when the evidence is weak. This
module is the model-free kernel:

    evidence → signals → ensemble raw score → calibrated confidence → capitalization gap →
    lifecycle → threshold / abstain → TrendReport

It makes no accuracy claim by itself. Whether it *works as intended* is decided by
:mod:`agentic_os.trend_backtest` — does a high-confidence threshold beat a momentum / search-only
baseline on historical replay, with useful lead time? — not by assertion (the plan's §28/§30).

Pure Python, no numpy: portable and deterministic, so a backtest is reproducible.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


# ── evidence + candidate (plan §4/§5) ──────────────────────────────────────────────
@dataclass(frozen=True)
class TrendEvidence:
    """One immutable observation grounding a signal (LLM summaries are NOT evidence, §4)."""
    source: str
    source_type: str
    observed_at: float
    entity: str
    normalized_metric: float
    platform: str = ""
    geo: str = ""
    language: str = ""
    content_hash: str = ""
    url: str = ""


@dataclass(frozen=True)
class TrendCandidate:
    """A topic under assessment. ``series`` maps each independent source → equally-spaced normalized
    volume samples over the observation window (oldest→newest). The rest are the plan's other signals
    and the competition/supply/manipulation inputs."""
    entity: str
    series: Mapping[str, Sequence[float]]                 # source → volume timeseries (oldest→newest)
    creator_outliers: Tuple[float, ...] = ()             # small-creator actual/expected ratios (§18)
    question_growth: float = 0.0                         # audience-question WoW growth (§17), 0..~2
    geo_count: int = 1                                   # distinct geographies showing it (§2)
    # competition / capitalization inputs (§9), each 0..1 (1 = fully saturated/competed)
    content_supply: float = 0.0
    competitor_authority: float = 0.0
    ad_density: float = 0.0
    keyword_competition: float = 0.0
    brand_penetration: float = 0.0
    # manipulation/noise inputs (§25), each 0..1 (1 = strong manipulation signal)
    bot_ratio: float = 0.0
    single_source_spike: bool = False
    seasonal_recurrence: float = 0.0


class LifecycleStage(Enum):
    LATENT = "latent"
    EMERGING = "emerging"
    ACCELERATING = "accelerating"
    BREAKOUT = "breakout"
    PEAKING = "peaking"
    SATURATED = "saturated"
    DECLINING = "declining"


# ── signal detectors (pure; §5) ─────────────────────────────────────────────────────
def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _growth_rate(vals: Sequence[float]) -> float:
    """Mean step-over-step relative growth over the window (a low-base-safe velocity)."""
    if len(vals) < 2:
        return 0.0
    rates = []
    for a, b in zip(vals, vals[1:]):
        base = a if a > 1e-9 else 1e-9
        rates.append((b - a) / base)
    return sum(rates) / len(rates)


def _acceleration(vals: Sequence[float]) -> float:
    """Is growth speeding up: mean second difference of the (smoothed) series, normalized."""
    if len(vals) < 3:
        return 0.0
    diffs = [b - a for a, b in zip(vals, vals[1:])]
    accels = [b - a for a, b in zip(diffs, diffs[1:])]
    scale = (max(vals) - min(vals)) or 1.0
    return sum(accels) / len(accels) / scale


def velocity(c: TrendCandidate) -> float:
    rates = [_growth_rate(v) for v in c.series.values() if len(v) >= 2]
    return _clamp(sum(rates) / len(rates)) if rates else 0.0


def acceleration(c: TrendCandidate) -> float:
    accs = [_acceleration(v) for v in c.series.values() if len(v) >= 3]
    return _clamp((sum(accs) / len(accs)) * 5.0) if accs else 0.0   # ×5: bring typical accel into 0..1


def cross_source_confirmation(c: TrendCandidate) -> float:
    """Independent CONFIRMATION — the *count* of sources showing upward emergence, targeting 3+.
    A lone rising source is weak (1/3); the signal is several sources agreeing, so this is what
    separates a real trend from a one-platform spike."""
    rising = sum(1 for v in c.series.values() if _growth_rate(v) > 0.05)
    return _clamp(min(rising, 3) / 3.0)


def source_independence(c: TrendCandidate) -> float:
    """1 - concentration of recent volume in a single source (Herfindahl-style). A topic carried by
    one source is fragile; several independent sources is the strong signal."""
    latest = {s: (v[-1] if v else 0.0) for s, v in c.series.items()}
    total = sum(latest.values())
    if total <= 0 or len(latest) <= 1:
        return 0.0
    shares = [x / total for x in latest.values()]
    hhi = sum(s * s for s in shares)                     # 1/n (spread) .. 1 (one source)
    n = len(shares)
    return _clamp((1 - hhi) / (1 - 1.0 / n)) if n > 1 else 0.0


def creator_outlier_strength(c: TrendCandidate) -> float:
    """Small creators dramatically beating their baseline — requires MULTIPLE examples (§18/§41)."""
    strong = [r for r in c.creator_outliers if r >= 3.0]
    if len(strong) < 2:
        return 0.0
    avg = sum(strong) / len(strong)
    return _clamp(math.log(avg) / math.log(20.0))        # 3× → ~0.37, 20× → 1.0


def audience_question_growth(c: TrendCandidate) -> float:
    return _clamp(c.question_growth / 1.5)


def geographic_spread(c: TrendCandidate) -> float:
    return _clamp((c.geo_count - 1) / 5.0)               # 1 geo → 0, 6+ → 1


def competition(c: TrendCandidate) -> float:
    return _clamp((c.competitor_authority + c.keyword_competition + c.brand_penetration) / 3.0)


def saturation(c: TrendCandidate) -> float:
    return _clamp((c.content_supply + c.ad_density) / 2.0)


def manipulation_risk(c: TrendCandidate) -> float:
    risk = 0.6 * c.bot_ratio + 0.3 * c.seasonal_recurrence
    if c.single_source_spike:
        risk += 0.4
    return _clamp(risk)


def source_concentration(c: TrendCandidate) -> float:
    return _clamp(1.0 - source_independence(c))


# ── ensemble future-confidence (plan §6) ────────────────────────────────────────────
#: Plausible a-priori weights (NOT fit to any benchmark — the backtest must not be circular).
DEFAULT_WEIGHTS: Dict[str, float] = {
    "emergence_velocity": 0.16, "acceleration": 0.14, "cross_source_confirmation": 0.16,
    "source_independence": 0.12, "creator_outlier_strength": 0.10, "audience_question_growth": 0.08,
    "geographic_spread": 0.06, "historical_pattern_similarity": 0.08,
    "competition": 0.10, "saturation": 0.08, "manipulation_risk": 0.14, "source_concentration": 0.08,
}
_POSITIVE = ("emergence_velocity", "acceleration", "cross_source_confirmation", "source_independence",
             "creator_outlier_strength", "audience_question_growth", "geographic_spread",
             "historical_pattern_similarity")
_NEGATIVE = ("competition", "saturation", "manipulation_risk", "source_concentration")


def signal_vector(c: TrendCandidate) -> Dict[str, float]:
    return {
        "emergence_velocity": velocity(c),
        "acceleration": acceleration(c),
        "cross_source_confirmation": cross_source_confirmation(c),
        "source_independence": source_independence(c),
        "creator_outlier_strength": creator_outlier_strength(c),
        "audience_question_growth": audience_question_growth(c),
        "geographic_spread": geographic_spread(c),
        "historical_pattern_similarity": _emergence_shape(c),
        "competition": competition(c),
        "saturation": saturation(c),
        "manipulation_risk": manipulation_risk(c),
        "source_concentration": source_concentration(c),
    }


def _emergence_shape(c: TrendCandidate) -> float:
    """Reward the classic early-emergence shape (rising from a low base) over a flat or spent curve."""
    merged = [sum(vals) for vals in zip(*[v for v in c.series.values() if v])] if c.series else []
    if len(merged) < 3:
        return 0.0
    first_third = sum(merged[: len(merged) // 3]) or 1e-9
    last_third = sum(merged[-len(merged) // 3:])
    ratio = last_third / first_third
    return _clamp(math.log(ratio) / math.log(10.0)) if ratio > 1 else 0.0


def raw_future_confidence(c: TrendCandidate, weights: Optional[Mapping[str, float]] = None) -> float:
    w = weights or DEFAULT_WEIGHTS
    sig = signal_vector(c)
    score = sum(w.get(k, 0.0) * sig[k] for k in _POSITIVE) - sum(w.get(k, 0.0) * sig[k] for k in _NEGATIVE)
    pos_max = sum(w.get(k, 0.0) for k in _POSITIVE) or 1.0
    return _clamp(score / pos_max)                       # normalize to 0..1 by the positive ceiling


# ── calibration (plan §6): isotonic regression via pool-adjacent-violators ──────────
@dataclass
class IsotonicCalibrator:
    """Map raw scores → a calibrated probability that means 'historical candidates scored near this
    crossed the breakout threshold at ~this rate' (§6). Fit on (score, outcome) pairs; monotone."""
    _xs: List[float] = field(default_factory=list)
    _ys: List[float] = field(default_factory=list)
    fitted: bool = False

    def fit(self, scores: Sequence[float], outcomes: Sequence[int]) -> "IsotonicCalibrator":
        pairs = sorted(zip(scores, [float(o) for o in outcomes]), key=lambda p: p[0])
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        wts = [1.0] * len(ys)
        # PAV: enforce non-decreasing ys by pooling adjacent violators
        i = 0
        blocks = [[y, w, x] for y, w, x in zip(ys, wts, xs)]  # [mean, weight, x-right]
        merged: List[List[float]] = []
        for b in blocks:
            merged.append(b[:])
            while len(merged) > 1 and merged[-2][0] > merged[-1][0]:
                y2, w2, x2 = merged.pop()
                y1, w1, x1 = merged.pop()
                w = w1 + w2
                merged.append([(y1 * w1 + y2 * w2) / w, w, x2])
        self._xs, self._ys = [], []
        for mean, _w, xr in merged:
            self._xs.append(xr)
            self._ys.append(_clamp(mean))
        self.fitted = True
        return self

    def transform(self, score: float) -> float:
        if not self.fitted or not self._xs:
            return _clamp(score)                          # identity until fit
        if score <= self._xs[0]:
            return self._ys[0]
        if score >= self._xs[-1]:
            return self._ys[-1]
        for i in range(1, len(self._xs)):
            if score <= self._xs[i]:
                x0, x1 = self._xs[i - 1], self._xs[i]
                y0, y1 = self._ys[i - 1], self._ys[i]
                t = (score - x0) / (x1 - x0) if x1 > x0 else 0.0
                return _clamp(y0 + t * (y1 - y0))
        return self._ys[-1]


# ── capitalization gap (plan §9) ────────────────────────────────────────────────────
def capitalization_gap(c: TrendCandidate) -> float:
    """High emerging demand + low supply/competition ⇒ a large, still-open gap (0..1)."""
    demand = 0.5 * velocity(c) + 0.3 * acceleration(c) + 0.2 * audience_question_growth(c)
    crowd = (competition(c) + saturation(c)) / 2.0
    return _clamp(demand * (1.0 - crowd))


# ── lifecycle (plan §2) ──────────────────────────────────────────────────────────────
def lifecycle(c: TrendCandidate) -> LifecycleStage:
    v, a, sat = velocity(c), acceleration(c), saturation(c)
    if sat >= 0.75:
        return LifecycleStage.SATURATED if v >= 0 else LifecycleStage.DECLINING
    if v < 0:
        return LifecycleStage.DECLINING
    if v < 0.03 and a < 0.1:
        return LifecycleStage.LATENT
    if a >= 0.5 and v >= 0.25:
        return LifecycleStage.BREAKOUT
    if a >= 0.3:
        return LifecycleStage.ACCELERATING
    if sat >= 0.5:
        return LifecycleStage.PEAKING
    return LifecycleStage.EMERGING


# ── precision/recall modes + the report (plan §7) ───────────────────────────────────
class Mode(Enum):
    STRICT = "strict"           # 0.90–0.99 — filter aggressively, abstain on weak evidence
    BALANCED = "balanced"       # 0.75–0.90
    EXPLORATORY = "exploratory" # 0.55–0.75


_MODE_THRESHOLD = {Mode.STRICT: 0.90, Mode.BALANCED: 0.75, Mode.EXPLORATORY: 0.55}
_MIN_SOURCES = 2                # evidence too thin below this ⇒ abstain regardless of score
_MAX_MANIPULATION = 0.6


@dataclass(frozen=True)
class TrendReport:
    entity: str
    raw_score: float
    confidence: float                       # calibrated
    lifecycle: LifecycleStage
    capitalization_gap: float
    signals: Mapping[str, float]
    abstained: bool
    reasons: Tuple[str, ...] = ()


def assess(c: TrendCandidate, *, calibrator: Optional[IsotonicCalibrator] = None,
           mode: Mode = Mode.BALANCED, weights: Optional[Mapping[str, float]] = None) -> TrendReport:
    """Score a candidate and decide whether to surface it (or abstain). Confidence is calibrated
    when a fitted calibrator is supplied; otherwise it is the raw score (identity)."""
    raw = raw_future_confidence(c, weights)
    conf = calibrator.transform(raw) if (calibrator and calibrator.fitted) else raw
    sig = signal_vector(c)
    reasons: List[str] = []
    threshold = _MODE_THRESHOLD[mode]
    if len([v for v in c.series.values() if v]) < _MIN_SOURCES:
        reasons.append("evidence too thin (needs ≥2 independent sources)")
    if sig["manipulation_risk"] > _MAX_MANIPULATION:
        reasons.append(f"manipulation risk {sig['manipulation_risk']:.2f} above {_MAX_MANIPULATION}")
    if conf < threshold:
        reasons.append(f"confidence {conf:.2f} below the {mode.value} threshold {threshold:.2f}")
    return TrendReport(entity=c.entity, raw_score=raw, confidence=conf, lifecycle=lifecycle(c),
                       capitalization_gap=capitalization_gap(c), signals=sig,
                       abstained=bool(reasons), reasons=tuple(reasons))
