"""Prospective H confirmation battery — multi-seed, larger-n, run as a GATE before expanding the Lab.

This is the shift from benchmark construction to hypothesis confirmation. The battery runs the prospective H
protocol (fix the S1 prediction from the held-out floor and model S0, then reveal S1) across every world and
several seeds, and asks a pre-registered question:

    Do the near-floor (null-control) worlds keep showing ~zero Learn lift, while the large-gap worlds keep
    capturing at least the pre-registered fraction of their systematic misalignment?

The null controls (Fraud A/B) are the strongest evidence: if a model already at the evidence floor keeps
showing no lift while misaligned worlds improve, "Learn just helps by feeding the model more information"
becomes hard to sustain. Conversely, a large Fraud lift at systematic≈0, or a large-gap world repeatedly
missing the capture threshold, falsifies H.

The battery SPEC (worlds, seeds, n, decision rule) is frozen as an immutable manifest; the live results are
recorded as a dated artifact (non-deterministic, so reported with mean±sd, not asserted in goldens). The
deterministic self-test below validates the machinery + gate logic reproducibly.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from .harness import DecisionCase, Proposal, World, control_arm, learned_arm
from .misalignment import (
    CAPTURE_THRESHOLD, NEGLIGIBLE_SYSTEMATIC_SHARE, predict_learn_opportunity, reveal_learn_outcome)

BATTERY_VERSION = "prospective-h-battery/v1"


@dataclass(frozen=True)
class BatterySpec:
    version: str = BATTERY_VERSION
    seeds: tuple[int, ...] = (11, 23, 37, 53, 71)
    n_eval: int = 32
    floor_fit_n: int = 2000
    capture_threshold: float = CAPTURE_THRESHOLD              # positive worlds: remove >= this of systematic
    negligible_systematic_share: float = NEGLIGIBLE_SYSTEMATIC_SHARE
    seed_pass_fraction: float = 0.8                           # a world passes if >= this share of seeds hold

    def as_dict(self) -> dict:
        return {"version": self.version, "seeds": list(self.seeds), "n_eval": self.n_eval,
                "floor_fit_n": self.floor_fit_n, "capture_threshold": self.capture_threshold,
                "negligible_systematic_share": self.negligible_systematic_share,
                "seed_pass_fraction": self.seed_pass_fraction}


@dataclass(frozen=True)
class WorldBatteryResult:
    world_id: str
    n_seeds: int
    mean_floor: float
    mean_s0: float
    sd_s0: float
    mean_systematic: float
    mean_s1: float
    sd_s1: float
    predict_null_fraction: float          # share of seeds the predictor called ~null (S0 near floor)
    held_fraction: float                  # share of seeds the pre-registered prediction held
    mean_capture: float                   # mean lift/systematic over positive-prediction seeds (nan-safe: -1 if none)
    per_seed: tuple[dict, ...]
    passed: bool

    def as_dict(self) -> dict:
        return {"world_id": self.world_id, "n_seeds": self.n_seeds,
                "mean_floor": round(self.mean_floor, 1), "mean_s0": round(self.mean_s0, 1),
                "sd_s0": round(self.sd_s0, 1), "mean_systematic": round(self.mean_systematic, 1),
                "mean_s1": round(self.mean_s1, 1), "sd_s1": round(self.sd_s1, 1),
                "predict_null_fraction": round(self.predict_null_fraction, 3),
                "held_fraction": round(self.held_fraction, 3),
                "mean_capture_positive_seeds": round(self.mean_capture, 3),
                "passed": self.passed, "per_seed": list(self.per_seed)}


def run_world_battery(world: World, s0_arm: Callable[[DecisionCase], Proposal],
                      s1_arm_for_seed: Callable[[int], Callable[[DecisionCase], Proposal]],
                      spec: BatterySpec) -> WorldBatteryResult:
    """Run the prospective H protocol across seeds for one world.

    ``s0_arm`` is the no-experience arm (seed-independent). ``s1_arm_for_seed(seed)`` builds the
    with-experience arm from that seed's training partition. For each seed the prediction is fixed from the
    held-out floor + S0 before S1 is revealed.
    """
    per_seed: list[dict] = []
    for seed in spec.seeds:
        pred = predict_learn_opportunity(world, s0_arm, seed=seed, n_eval=spec.n_eval,
                                         floor_fit_n=spec.floor_fit_n)
        revealed = reveal_learn_outcome(pred, world, s1_arm_for_seed(seed), seed=seed, n_eval=spec.n_eval)
        per_seed.append(revealed.as_dict())

    s0s = [r["model_s0_regret"] for r in per_seed]
    s1s = [r["model_s1_regret"] for r in per_seed]
    caps = [r["capture_ratio"] for r in per_seed if not r["predict_null"] and r["capture_ratio"] is not None]
    held = [bool(r["hypothesis_held"]) for r in per_seed]
    return WorldBatteryResult(
        world_id=world.world_id, n_seeds=len(spec.seeds),
        mean_floor=statistics.mean(r["evidence_floor_regret"] for r in per_seed),
        mean_s0=statistics.mean(s0s), sd_s0=statistics.pstdev(s0s) if len(s0s) > 1 else 0.0,
        mean_systematic=statistics.mean(r["systematic_misalignment"] for r in per_seed),
        mean_s1=statistics.mean(s1s), sd_s1=statistics.pstdev(s1s) if len(s1s) > 1 else 0.0,
        predict_null_fraction=sum(1 for r in per_seed if r["predict_null"]) / len(per_seed),
        held_fraction=sum(held) / len(held),
        mean_capture=(statistics.mean(caps) if caps else -1.0),
        per_seed=tuple(per_seed),
        passed=(sum(held) / len(held)) >= spec.seed_pass_fraction)


def gate_verdict(results: Sequence[WorldBatteryResult], spec: BatterySpec) -> dict:
    """The pre-registered gate. H SURVIVES iff every world passes its seed-pass bar — which, per world type,
    means null-control worlds kept ~zero lift and large-gap worlds kept capturing >= the threshold. Also
    surfaces the two explicit falsifiers: a null world with real lift, or a large-gap world that missed."""
    null_worlds = [r for r in results if r.predict_null_fraction >= 0.5]
    positive_worlds = [r for r in results if r.predict_null_fraction < 0.5]
    falsifiers = []
    for r in null_worlds:
        if not r.passed:
            falsifiers.append(f"NULL-CONTROL BREACH: {r.world_id} showed lift despite systematic≈0 "
                              f"(held {r.held_fraction:.0%} of seeds)")
    for r in positive_worlds:
        if not r.passed:
            falsifiers.append(f"CAPTURE MISS: {r.world_id} failed the ≥{spec.capture_threshold:.0%} "
                              f"capture threshold (held {r.held_fraction:.0%} of seeds)")
    survives = all(r.passed for r in results) and not falsifiers
    return {
        "battery_version": spec.version, "survives": survives,
        "null_control_worlds": [r.world_id for r in null_worlds],
        "positive_worlds": [r.world_id for r in positive_worlds],
        "falsifiers": falsifiers,
        "per_world_pass": {r.world_id: r.passed for r in results},
    }


# --------------------------------------------------------------------------- deterministic machinery self-test
def deterministic_battery(world: World, spec: BatterySpec) -> WorldBatteryResult:
    """Reproducible machinery check: baseline as the S0 proxy, per-seed learned policy as the S1 proxy.
    Not the frozen model — this exists to validate the battery + gate logic deterministically."""
    def s1_for_seed(seed: int) -> Callable[[DecisionCase], Proposal]:
        return learned_arm(world, world.cases(seed=seed, n=spec.floor_fit_n))
    return run_world_battery(world, control_arm(world), s1_for_seed, spec)
