"""Safe-Learn — an auditable abstention guard that applies verified Experience only where it is supported.

v2 confirmed H's opportunity+capture core but found Learn is *not safe* on Stale-Quote (19.3% of decisions
materially worse). This introduces a third arm without touching S0/S1:

    S0 = frozen model, no Experience
    S1 = frozen model + Experience  (current Learn)
    S2 = S1 where the guard ACCEPTS the lesson, else fall back to S0

The guard is deliberately **deterministic and auditable**, not another learned confidence model. It reads only
properties of the *training* Experience for a case's observable bucket:

    support(b)      = number of training cases in bucket b
    reliability(b)  = P(lesson_action(b) == per-case optimal | training cases in b)   (1 − counterexample rate)

and ACCEPTS iff `support(b) ≥ min_support AND reliability(b) ≥ τ`. Because S2 chooses between two
already-evaluated actions, its per-case normalized regret is simply `s1_rn` on accepted cases and `s0_rn` on
abstained ones — no re-evaluation, no extra model calls.

Restated hypothesis this arm tests:
  * **H3 (safety)** — positive average capture does not imply safe per-decision improvement; learned
    knowledge needs applicability-aware abstention. Concretely: can a support/reliability guard keep most of a
    world's capture while removing most of its harmful tail? If getting harm < 5% also destroys the benefit,
    harmful and useful applications are *not separable* from the current evidence — a Discovery signal, not a
    call for fancier confidence heuristics.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from .harness import DecisionCase, World, learned_arm
from .misalignment import bucket_oracle_policy

MATERIAL = 0.10          # a case is "materially worse" if normalized regret rises by > this (matches v2)


@dataclass(frozen=True)
class BucketReliability:
    support: int
    reliability: float          # fraction of training cases in the bucket the lesson action gets optimal
    lesson_action: str


def lesson_reliability(world: World, train: Sequence[DecisionCase]) -> dict[str, BucketReliability]:
    """Per observable bucket: support, the lesson's action, and how often that action is the per-case optimum
    on training cases. All from training Experience only."""
    from collections import defaultdict
    members: dict[str, list[DecisionCase]] = defaultdict(list)
    for c in train:
        members[world.bucket(c.observable)].append(c)
    policy = bucket_oracle_policy(world, train)             # the learned per-bucket best action
    out: dict[str, BucketReliability] = {}
    for b, cs in members.items():
        act = policy.get(b, world.default_hold())
        hits = sum(1 for c in cs if world.optimal(c.latent) == act)
        out[b] = BucketReliability(support=len(cs), reliability=hits / len(cs), lesson_action=act)
    return out


@dataclass(frozen=True)
class AbstentionGuard:
    reliability: Mapping[str, BucketReliability]
    min_support: int
    tau: float

    def accept(self, bucket: str) -> bool:
        r = self.reliability.get(bucket)
        return bool(r and r.support >= self.min_support and r.reliability >= self.tau)


def s2_normalized_regret(rec: Mapping, guard: AbstentionGuard) -> float:
    """S2's per-case normalized regret: S1's when the guard accepts the bucket, else S0's."""
    return rec["s1_rn"] if guard.accept(rec["bucket"]) else rec["s0_rn"]


# --------------------------------------------------------------------------- risk–coverage sweep + metrics
def risk_coverage(records: Sequence[Mapping], reliability: Mapping[str, BucketReliability], *,
                  min_support: int, taus: Sequence[float]) -> list[dict]:
    """Sweep τ (on a validation split) → coverage, benefit vs S0, and material-worse(S2 vs S0) rate."""
    rows = []
    for tau in taus:
        g = AbstentionGuard(reliability, min_support, tau)
        n = len(records) or 1
        accepted = [r for r in records if g.accept(r["bucket"])]
        s2 = [s2_normalized_regret(r, g) for r in records]
        benefit = statistics.mean(r["s0_rn"] - s2v for r, s2v in zip(records, s2))
        worse = sum(1 for r, s2v in zip(records, s2) if s2v - r["s0_rn"] > MATERIAL) / n
        rows.append({"tau": round(tau, 3), "coverage": round(len(accepted) / n, 4),
                     "benefit_vs_s0": round(benefit, 4), "material_worse_vs_s0": round(worse, 4)})
    return rows


def select_tau(rc_rows: Sequence[dict], *, max_material_worse: float = 0.05) -> float:
    """Seal one τ on validation: the smallest τ (max coverage) whose material-worse rate is under the bar;
    if none qualifies, the most conservative τ available."""
    ok = [r for r in rc_rows if r["material_worse_vs_s0"] <= max_material_worse]
    if ok:
        return min(ok, key=lambda r: r["tau"])["tau"]
    return max(rc_rows, key=lambda r: r["tau"])["tau"]


def arm_metrics(records: Sequence[Mapping], arm_rn: Callable[[Mapping], float], *,
                s0_key: str = "s0_rn") -> dict:
    n = len(records) or 1
    vals = [arm_rn(r) for r in records]
    worse = sum(1 for r, v in zip(records, vals) if v - r[s0_key] > MATERIAL) / n
    return {"mean_norm_regret": round(statistics.mean(vals), 4),
            "mean_delta_vs_s0": round(statistics.mean(r[s0_key] - v for r, v in zip(records, vals)), 4),
            "material_worse_vs_s0": round(worse, 4),
            "loss_rate_vs_s0": round(sum(1 for r, v in zip(records, vals) if v - r[s0_key] > 1e-9) / n, 4)}


def evaluate_s2(records: Sequence[Mapping], guard: AbstentionGuard) -> dict:
    """Full S2 held-out report incl. capture preservation and selectivity (accept vs abstain harm)."""
    n = len(records) or 1
    s1 = arm_metrics(records, lambda r: r["s1_rn"])
    s2 = arm_metrics(records, lambda r: s2_normalized_regret(r, guard))
    accepted = [r for r in records if guard.accept(r["bucket"])]
    abstained = [r for r in records if not guard.accept(r["bucket"])]
    def would_be_harm(rs):   # material-worse(S1 vs S0) that would have occurred on this subset
        return round(sum(1 for r in rs if r["s1_rn"] - r["s0_rn"] > MATERIAL) / (len(rs) or 1), 4)
    lift_s1 = s1["mean_delta_vs_s0"]
    lift_s2 = s2["mean_delta_vs_s0"]
    return {
        "coverage": round(len(accepted) / n, 4),
        "S1": s1, "S2": s2,
        "capture_preservation": (round(lift_s2 / lift_s1, 4) if lift_s1 > 1e-9 else None),
        "selectivity": {"harm_if_accepted": would_be_harm(accepted),
                        "harm_avoided_on_abstained": would_be_harm(abstained)},
    }


# --------------------------------------------------------------------------- loss diagnosis (7 categories)
LOSS_CATEGORIES = (
    "SUPPORT_WEAKNESS", "EVIDENCE_INSUFFICIENCY", "BUCKET_AGGREGATION", "TEMPORAL_MISMATCH",
    "ACTION_MISMATCH", "OUTCOME_VARIANCE", "CONTEXT_MISMATCH")


def diagnose_losses(records: Sequence[Mapping], reliability: Mapping[str, BucketReliability], *,
                    min_support: int = 20) -> dict:
    """Partition the cases where S1 is materially worse than S0 into auditable categories, to see whether the
    harm is abstainable (support/reliability) or intrinsic (unlucky/model-deviation/evidence floor)."""
    from collections import Counter
    losses = [r for r in records if r["s1_rn"] - r["s0_rn"] > MATERIAL]
    cats = Counter()
    for r in losses:
        rel = reliability.get(r["bucket"])
        if rel is None or rel.support < min_support:
            cats["SUPPORT_WEAKNESS"] += 1
        elif r["s1_action"] != rel.lesson_action:
            cats["ACTION_MISMATCH"] += 1                    # the model didn't follow the lesson
        elif rel.reliability < 0.5:
            cats["EVIDENCE_INSUFFICIENCY"] += 1             # bucket genuinely ambiguous; can't be confident
        elif rel.reliability < 0.8:
            cats["BUCKET_AGGREGATION"] += 1                 # good on average, wrong for this sub-state
        elif int(r.get("prior_touches", 0)) >= 3:
            cats["TEMPORAL_MISMATCH"] += 1                  # fatigue regime the bucket underweights
        else:
            cats["OUTCOME_VARIANCE"] += 1                   # reliable lesson, unlucky latent
    abstainable = sum(cats[c] for c in ("SUPPORT_WEAKNESS", "EVIDENCE_INSUFFICIENCY", "BUCKET_AGGREGATION"))
    return {"n_losses": len(losses), "by_category": dict(cats),
            "abstainable_share": round(abstainable / (len(losses) or 1), 4)}
