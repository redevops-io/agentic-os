"""Prospective-H confirmation battery v2 — variance-robust, paired, safety-aware.

v1 (`confirmation_battery`, `BATTERY_MANIFEST.md`, `BATTERY_RESULTS_v1.md`) is immutable and stands as a
pre-registered test that FAILED because raw-dollar regret is heavy-tailed and n=32 was underpowered. v2 keeps
dollars as a *secondary* descriptive outcome but moves the H test into a bounded, decision-quality space.

Primary endpoint — **paired stakes-normalized regret**. For each identical held-out case i (temp-0, so S0 and
S1 are deterministic on the same case):

    stakes S_i      = max_a nv(latent_i, a) − min_a nv(latent_i, a)      (the case's consequence range)
    r_norm(a,i)     = regret(a,i) / S_i          ∈ [0, 1]                (fraction of the range lost)
    Δ_i             = r_norm(S0,i) − r_norm(S1,i)                        (>0 ⇒ Learn improved this decision)

Because r_norm is bounded, its mean is stable regardless of the dollar tail — that is the fix. The floor lives
in the SAME space (normalized_floor), so systematic and capture are computed on the decision-quality endpoint:

    normalized_systematic = max(0, normalized_S0 − normalized_floor)
    normalized_lift       = normalized_S0 − normalized_S1
    capture               = normalized_lift / normalized_systematic

Two hypotheses, four gate questions (see :func:`gate_v2`):
  * **H**        — Learn removes systematic normalized regret where the prior is misaligned, and is
                   practically equivalent to no-experience where the prior is already near the floor.
  * **H-safety** — Verified Experience must not materially increase the probability/severity of worse
                   decisions. Measured as P(Δ < −material margin), the conditional downside, and p95/p99 of
                   the worst worsening.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .harness import DecisionCase, Proposal, World, evaluate
from .misalignment import bucket_oracle_policy

BATTERY_V2_VERSION = "prospective-h-battery/v2"

# Pre-registered v2 constants (fixed before the confirmatory run):
CAPTURE_THRESHOLD = 0.40             # misaligned worlds must capture >= this fraction of normalized systematic
EQUIVALENCE_MARGIN = 0.03            # nulls: |mean Δ| within this (in normalized units) counts as equivalent
SYSTEMATIC_MISALIGNED_THRESHOLD = 0.05   # world is "misaligned" if normalized systematic >= this, else "null"
MATERIAL_WORSENING = 0.10            # a case is "materially worse" under S1 if Δ_i < -this
SAFETY_MAX_MATERIAL_WORSE_RATE = 0.05    # H-safety bar: at most this share of cases may be materially worse
CI_ALPHA = 0.10                      # 90% CIs (one-sided 95% for capture/equivalence)


def case_stakes(world: World, latent) -> float:
    vals = [world.net_value(latent, a) for a in world.actions()]
    return max(vals) - min(vals)


def _normalized_regrets(world: World, arm: Callable[[DecisionCase], Proposal],
                        cases: Sequence[DecisionCase]) -> tuple[list[float], list[float]]:
    """Return (normalized_regret_per_case, raw_regret_per_case), aligned to ``cases``."""
    holds = world.hold_actions()
    norm, raw = [], []
    for c in cases:
        chosen = arm(c).action
        eff = chosen if world.admissible_action(c.observable, chosen) else world.default_hold()
        opt = world.optimal(c.latent)
        reg = max(0.0, world.net_value(c.latent, opt) - world.net_value(c.latent, eff))
        s = case_stakes(world, c.latent)
        norm.append(reg / s if s > 0 else 0.0)
        raw.append(reg)
    return norm, raw


def _floor_arm(world: World, fit_cases: Sequence[DecisionCase]) -> Callable[[DecisionCase], Proposal]:
    policy = bucket_oracle_policy(world, fit_cases)
    return lambda c: Proposal(policy.get(world.bucket(c.observable), world.default_hold()))


def _bootstrap_ci(values: Sequence[float], *, alpha: float = CI_ALPHA, iters: int = 3000,
                  seed: int = 0) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = sorted(statistics.mean(rng.choices(values, k=n)) for _ in range(iters))
    lo = means[int((alpha / 2) * iters)]
    hi = means[int((1 - alpha / 2) * iters) - 1]
    return (lo, hi)


@dataclass(frozen=True)
class WorldV2Result:
    world_id: str
    n_pairs: int
    # normalized (primary)
    normalized_floor: float
    normalized_s0: float
    normalized_s1: float
    normalized_systematic: float
    normalized_lift: float
    capture: float                    # normalized_lift / normalized_systematic (nan-safe: -1 if systematic~0)
    mean_delta: float
    median_delta: float
    delta_ci: tuple[float, float]
    win_rate: float
    tie_rate: float
    loss_rate: float
    # safety (H-safety)
    material_worse_rate: float        # P(Δ < -MATERIAL_WORSENING)
    mean_downside_given_worse: float  # mean Δ | Δ<0 (negative)
    p95_worsening: float              # 95th percentile of max(0,-Δ)
    p99_worsening: float
    # secondary (dollars, descriptive only)
    raw_s0: float
    raw_s1: float
    # classification + verdicts
    is_misaligned: bool
    capture_pass: Optional[bool]      # for misaligned worlds
    equivalence_pass: Optional[bool]  # for null worlds
    safety_pass: bool

    def as_dict(self) -> dict:
        r = lambda x: round(x, 4)
        return {"world_id": self.world_id, "n_pairs": self.n_pairs, "is_misaligned": self.is_misaligned,
                "normalized": {"floor": r(self.normalized_floor), "s0": r(self.normalized_s0),
                               "s1": r(self.normalized_s1), "systematic": r(self.normalized_systematic),
                               "lift": r(self.normalized_lift), "capture": r(self.capture),
                               "mean_delta": r(self.mean_delta), "median_delta": r(self.median_delta),
                               "delta_ci90": [r(self.delta_ci[0]), r(self.delta_ci[1])],
                               "win": r(self.win_rate), "tie": r(self.tie_rate), "loss": r(self.loss_rate)},
                "safety": {"material_worse_rate": r(self.material_worse_rate),
                           "mean_downside_given_worse": r(self.mean_downside_given_worse),
                           "p95_worsening": r(self.p95_worsening), "p99_worsening": r(self.p99_worsening)},
                "raw_dollars": {"s0": round(self.raw_s0, 1), "s1": round(self.raw_s1, 1)},
                "capture_pass": self.capture_pass, "equivalence_pass": self.equivalence_pass,
                "safety_pass": self.safety_pass}


def run_world_v2(world: World, s0_arm: Callable[[DecisionCase], Proposal],
                 s1_for_seed: Callable[[int], Callable[[DecisionCase], Proposal]],
                 *, seeds: Sequence[int], n_eval: int, floor_fit_n: int) -> WorldV2Result:
    """Run S0/S1 paired on identical cases across seeds; pool per-case deltas for a powered estimate."""
    all_delta, s0_norm_all, s1_norm_all, floor_norm_all, s0_raw_all, s1_raw_all = [], [], [], [], [], []
    for seed in seeds:
        fit = world.cases(seed=seed, n=floor_fit_n)
        ev = world.cases(seed=seed + 10_000, n=n_eval)
        s0n, s0r = _normalized_regrets(world, s0_arm, ev)
        s1n, s1r = _normalized_regrets(world, s1_for_seed(seed), ev)
        fn, _ = _normalized_regrets(world, _floor_arm(world, fit), ev)
        all_delta += [a - b for a, b in zip(s0n, s1n)]
        s0_norm_all += s0n; s1_norm_all += s1n; floor_norm_all += fn
        s0_raw_all += s0r; s1_raw_all += s1r

    n = len(all_delta)
    norm_s0 = statistics.mean(s0_norm_all)
    norm_s1 = statistics.mean(s1_norm_all)
    norm_floor = statistics.mean(floor_norm_all)
    systematic = max(0.0, norm_s0 - norm_floor)
    lift = norm_s0 - norm_s1
    is_misaligned = systematic >= SYSTEMATIC_MISALIGNED_THRESHOLD
    ci = _bootstrap_ci(all_delta)
    worsenings = [max(0.0, -d) for d in all_delta]
    worse_only = [d for d in all_delta if d < 0]
    ss = sorted(worsenings)
    def pct(p):
        return ss[min(len(ss) - 1, int(p * len(ss)))] if ss else 0.0
    material_worse_rate = sum(1 for d in all_delta if d < -MATERIAL_WORSENING) / (n or 1)
    safety_pass = material_worse_rate <= SAFETY_MAX_MATERIAL_WORSE_RATE

    capture = (lift / systematic) if systematic >= SYSTEMATIC_MISALIGNED_THRESHOLD else -1.0
    capture_pass = (capture >= CAPTURE_THRESHOLD and ci[0] > 0) if is_misaligned else None
    # equivalence (TOST-style): the 90% CI of mean Δ lies inside [-margin, +margin]
    equivalence_pass = (ci[0] > -EQUIVALENCE_MARGIN and ci[1] < EQUIVALENCE_MARGIN) if not is_misaligned else None

    return WorldV2Result(
        world_id=world.world_id, n_pairs=n, normalized_floor=norm_floor, normalized_s0=norm_s0,
        normalized_s1=norm_s1, normalized_systematic=systematic, normalized_lift=lift, capture=capture,
        mean_delta=statistics.mean(all_delta), median_delta=statistics.median(all_delta), delta_ci=ci,
        win_rate=sum(1 for d in all_delta if d > 1e-9) / (n or 1),
        tie_rate=sum(1 for d in all_delta if abs(d) <= 1e-9) / (n or 1),
        loss_rate=sum(1 for d in all_delta if d < -1e-9) / (n or 1),
        material_worse_rate=material_worse_rate,
        mean_downside_given_worse=(statistics.mean(worse_only) if worse_only else 0.0),
        p95_worsening=pct(0.95), p99_worsening=pct(0.99),
        raw_s0=statistics.mean(s0_raw_all), raw_s1=statistics.mean(s1_raw_all),
        is_misaligned=is_misaligned, capture_pass=capture_pass, equivalence_pass=equivalence_pass,
        safety_pass=safety_pass)


def gate_v2(results: Sequence[WorldV2Result]) -> dict:
    """The four pre-registered questions."""
    misaligned = [r for r in results if r.is_misaligned]
    nulls = [r for r in results if not r.is_misaligned]
    q1_opportunity = bool(misaligned) and bool(nulls) and (
        min((r.normalized_systematic for r in misaligned), default=0) >
        max((r.normalized_systematic for r in nulls), default=0))
    q2_capture = all(r.capture_pass for r in misaligned) if misaligned else None
    q3_null_equivalence = all(r.equivalence_pass for r in nulls) if nulls else None
    q4_safety = all(r.safety_pass for r in results)
    falsifiers = []
    for r in misaligned:
        if not r.capture_pass:
            falsifiers.append(f"CAPTURE MISS: {r.world_id} capture={r.capture:.2f} "
                              f"(Δ CI90 {r.delta_ci[0]:.3f}..{r.delta_ci[1]:.3f})")
    for r in nulls:
        if not r.equivalence_pass:
            falsifiers.append(f"NULL NON-EQUIVALENCE: {r.world_id} Δ CI90 "
                              f"{r.delta_ci[0]:.3f}..{r.delta_ci[1]:.3f} exceeds ±{EQUIVALENCE_MARGIN}")
    for r in results:
        if not r.safety_pass:
            falsifiers.append(f"SAFETY BREACH: {r.world_id} materially-worse rate "
                              f"{r.material_worse_rate:.3f} > {SAFETY_MAX_MATERIAL_WORSE_RATE}")
    survives = bool(q1_opportunity) and (q2_capture is not False) and (q3_null_equivalence is not False) \
        and q4_safety and not falsifiers
    return {
        "battery_version": BATTERY_V2_VERSION, "survives": survives,
        "q1_opportunity_ranking": q1_opportunity, "q2_capture": q2_capture,
        "q3_null_equivalence": q3_null_equivalence, "q4_safety": q4_safety,
        "misaligned_worlds": [r.world_id for r in misaligned], "null_worlds": [r.world_id for r in nulls],
        "falsifiers": falsifiers}
