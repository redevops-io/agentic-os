"""Prior-misalignment instrument — measuring, in every world, the quantity H says predicts Learn lift.

Promoted hypothesis (to be falsified, not assumed):

    H: Runtime-learning lift is proportional not to task difficulty, but to the presence of a stable,
       learnable mismatch between the frozen model's behavioral prior and the environment's actual economics.

To test H we must measure the mismatch *independently* of the lift, and separate two very different reasons
a decision can be wrong:

  * the **evidence floor** — the regret of the best policy any learner could realise from the observable
    alone (the omniscient-within-observable policy). This is *evidence insufficiency*: irreducible
    uncertainty (e.g. Stale-Quote's dead-vs-patient leads, both silent). No amount of Learn removes it.
  * the **systematic misalignment** — the prior's regret *above* that floor. This is *learning failure*:
    a fixable, learnable gap. H predicts Learn lift should track this, and can never exceed it.

``systematic = max(0, prior_regret - evidence_floor)`` is therefore the predicted ceiling on Learn lift.
Comparing it to observed lift across worlds is how we falsify H. With three or four worlds we do not fit a
line — we record the quantity in every world so that, as worlds accrue, we can see whether it predicts.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from .harness import DecisionCase, Proposal, World, control_arm, evaluate, learned_arm


def bucket_oracle_policy(world: World, cases: Sequence[DecisionCase]) -> dict[str, str]:
    """The omniscient-within-observable policy: per observable bucket, the action with the best TRUE mean
    net value. This is the ceiling any learner can reach from the given signal — reached by ``learned_arm``
    with unlimited in-distribution data."""
    from collections import defaultdict
    tally: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for c in cases:
        b = world.bucket(c.observable)
        for a in world.actions():
            if world.admissible_action(c.observable, a):
                tally[b][a].append(world.net_value(c.latent, a))
    return {b: max(per, key=lambda a: statistics.mean(per[a])) for b, per in tally.items()}


def evidence_floor_regret(world: World, cases: Sequence[DecisionCase], *,
                          fit_cases: Optional[Sequence[DecisionCase]] = None) -> float:
    """Estimated observable-policy floor: mean regret of the best observable-conditioned policy — the
    irreducible part (evidence insufficiency).

    When ``fit_cases`` is given, the policy is SELECTED on ``fit_cases`` and evaluated on ``cases`` — a
    held-out estimate on the same held-out set as S0/S1, so ``S0 − floor`` is a clean predictor. Without it
    the policy is fit and evaluated in-sample (an optimistic ceiling); prefer the held-out form for H.
    Unseen buckets fall back to the world's default HOLD.
    """
    policy = bucket_oracle_policy(world, fit_cases if fit_cases is not None else cases)
    def arm(c: DecisionCase) -> Proposal:
        return Proposal(policy.get(world.bucket(c.observable), world.default_hold()))
    return evaluate(world, arm, cases, arm_name="observable_policy_floor").mean_regret


@dataclass(frozen=True)
class PriorMisalignment:
    world_id: str
    prior_label: str
    prior_regret: float
    evidence_floor_regret: float
    systematic_misalignment: float     # prior_regret - floor: the fixable gap = predicted Learn ceiling
    learnable_share: float             # systematic / prior_regret (0..1): how much of the prior's error is fixable
    over_intervention_rate: float      # prior acted when the optimal action was restraint (HOLD-class)
    restraint_optimal_rate: float      # fraction of cases where the optimal action is restraint

    def as_dict(self) -> dict:
        return {"world_id": self.world_id, "prior_label": self.prior_label,
                "prior_regret": round(self.prior_regret, 2),
                "evidence_floor_regret": round(self.evidence_floor_regret, 2),
                "systematic_misalignment": round(self.systematic_misalignment, 2),
                "learnable_share": round(self.learnable_share, 4),
                "over_intervention_rate": round(self.over_intervention_rate, 4),
                "restraint_optimal_rate": round(self.restraint_optimal_rate, 4)}


def measure_misalignment(world: World, prior_arm: Callable[[DecisionCase], Proposal],
                         cases: Sequence[DecisionCase], *, prior_label: str,
                         fit_cases: Optional[Sequence[DecisionCase]] = None) -> PriorMisalignment:
    """Decompose a prior's regret into an irreducible evidence floor and a fixable systematic gap.

    ``prior_arm`` stands in for the behavioral prior whose misalignment we want. Deterministically we pass
    the world's naive baseline (a model-free proxy for the environment's structure); to test H against the
    *actual* frozen model, pass its no-experience (S0) arm and read ``systematic_misalignment`` as the
    predicted ceiling on the model's Learn lift.
    """
    rep = evaluate(world, prior_arm, cases, arm_name=prior_label)
    floor = evidence_floor_regret(world, cases, fit_cases=fit_cases)
    systematic = max(0.0, rep.mean_regret - floor)
    holds = world.hold_actions()
    restraint_opt = sum(1 for c in cases if world.optimal(c.latent) in holds) / (len(cases) or 1)
    return PriorMisalignment(
        world_id=world.world_id, prior_label=prior_label, prior_regret=rep.mean_regret,
        evidence_floor_regret=floor, systematic_misalignment=systematic,
        learnable_share=(systematic / rep.mean_regret) if rep.mean_regret > 0 else 0.0,
        over_intervention_rate=rep.unnecessary_intervention_rate, restraint_optimal_rate=restraint_opt)


# Pre-registered decision rule for the prospective H test (fixed BEFORE any world is run):
CAPTURE_THRESHOLD = 0.4          # Learn should remove at least this fraction of systematic misalignment
NEGLIGIBLE_SYSTEMATIC_SHARE = 0.15   # if systematic < this * S0, predict a ~null lift instead


@dataclass(frozen=True)
class ProspectiveHResult:
    """A prospective H test: the prediction is fixed from (held-out floor, model S0) BEFORE S1 is revealed."""
    world_id: str
    evidence_floor_regret: float
    model_s0_regret: float
    systematic_misalignment: float
    systematic_share: float
    predict_null: bool
    predicted_s1_ceiling: float      # H requires the revealed S1 to be at or below this
    prediction: str
    # revealed later:
    model_s1_regret: Optional[float] = None
    lift: Optional[float] = None
    capture_ratio: Optional[float] = None       # lift / systematic
    hypothesis_held: Optional[bool] = None

    def as_dict(self) -> dict:
        d = {"world_id": self.world_id, "evidence_floor_regret": round(self.evidence_floor_regret, 2),
             "model_s0_regret": round(self.model_s0_regret, 2),
             "systematic_misalignment": round(self.systematic_misalignment, 2),
             "systematic_share": round(self.systematic_share, 4), "predict_null": self.predict_null,
             "predicted_s1_ceiling": round(self.predicted_s1_ceiling, 2), "prediction": self.prediction}
        if self.model_s1_regret is not None:
            d.update(model_s1_regret=round(self.model_s1_regret, 2), lift=round(self.lift, 2),
                     capture_ratio=(round(self.capture_ratio, 3) if self.capture_ratio is not None else None),
                     hypothesis_held=self.hypothesis_held)
        return d


def predict_learn_opportunity(world: World, s0_arm: Callable[[DecisionCase], Proposal], *,
                              seed: int = 5, n_eval: int = 200, floor_fit_n: int = 2000
                              ) -> ProspectiveHResult:
    """Phase 1 (pre-registration): estimate the held-out observable-policy floor, run S0, and FIX the S1
    prediction — without touching S1. Record the returned object before calling :func:`reveal_learn_outcome`.
    """
    fit = world.cases(seed=seed, n=floor_fit_n)
    ev = world.cases(seed=seed + 10_000, n=n_eval)
    floor = evidence_floor_regret(world, ev, fit_cases=fit)
    s0 = evaluate(world, s0_arm, ev, arm_name="S0").mean_regret
    systematic = max(0.0, s0 - floor)
    share = systematic / s0 if s0 else 0.0
    predict_null = share < NEGLIGIBLE_SYSTEMATIC_SHARE
    ceiling = s0 if predict_null else s0 - CAPTURE_THRESHOLD * systematic
    prediction = (
        f"~NULL: model S0 already near floor (systematic share {share:.2f} < {NEGLIGIBLE_SYSTEMATIC_SHARE}); "
        f"expect S1 ≈ S0 ({s0:.0f})" if predict_null else
        f"Learn should remove ≥{CAPTURE_THRESHOLD:.0%} of systematic ({systematic:.0f}); "
        f"expect S1 ≤ {ceiling:.0f} (floor {floor:.0f})")
    return ProspectiveHResult(
        world_id=world.world_id, evidence_floor_regret=floor, model_s0_regret=s0,
        systematic_misalignment=systematic, systematic_share=share, predict_null=predict_null,
        predicted_s1_ceiling=ceiling, prediction=prediction)


def reveal_learn_outcome(prediction: ProspectiveHResult, world: World,
                         s1_arm: Callable[[DecisionCase], Proposal], *, seed: int = 5,
                         n_eval: int = 200) -> ProspectiveHResult:
    """Phase 2 (reveal): run S1 on the SAME held-out cases and test the pre-registered prediction."""
    from dataclasses import replace
    ev = world.cases(seed=seed + 10_000, n=n_eval)
    s1 = evaluate(world, s1_arm, ev, arm_name="S1").mean_regret
    lift = prediction.model_s0_regret - s1
    sysm = prediction.systematic_misalignment
    capture = (lift / sysm) if sysm > 0 else None
    if prediction.predict_null:
        held = abs(lift) < NEGLIGIBLE_SYSTEMATIC_SHARE * prediction.model_s0_regret + 1.0
    else:
        held = s1 <= prediction.predicted_s1_ceiling + 1e-6
    return replace(prediction, model_s1_regret=s1, lift=lift, capture_ratio=capture, hypothesis_held=held)


@dataclass(frozen=True)
class WorldLearnProfile:
    """Everything H needs from one world, measured deterministically: the evidence floor, the baseline
    prior's misalignment, and the realised deterministic learnable gap (baseline → learned)."""
    world_id: str
    evidence_floor_regret: float
    baseline_regret: float
    learned_regret: float
    baseline_misalignment: PriorMisalignment
    deterministic_learn_lift: float            # baseline_regret - learned_regret
    deterministic_learn_lift_share: float
    live_model: Optional[Mapping[str, float]] = None   # captured S0/S1 exploratory numbers, if provided

    def as_dict(self) -> dict:
        d = {"world_id": self.world_id,
             "evidence_floor_regret": round(self.evidence_floor_regret, 2),
             "baseline_regret": round(self.baseline_regret, 2),
             "learned_regret": round(self.learned_regret, 2),
             "deterministic_learn_lift": round(self.deterministic_learn_lift, 2),
             "deterministic_learn_lift_share": round(self.deterministic_learn_lift_share, 4),
             "baseline_misalignment": self.baseline_misalignment.as_dict()}
        if self.live_model:
            d["live_model_exploratory"] = dict(self.live_model)
        return d


def profile_world(world: World, *, seed: int = 7, n_train: int = 600, n_eval: int = 600,
                  live_model: Optional[Mapping[str, float]] = None) -> WorldLearnProfile:
    """Measure a world's H-relevant quantities on held-out cases (baseline prior; deterministic learn)."""
    train = world.cases(seed=seed, n=n_train)
    ev = world.cases(seed=seed + 10_000, n=n_eval)
    base = evaluate(world, control_arm(world), ev, arm_name="baseline")
    learned = evaluate(world, learned_arm(world, train), ev, arm_name="learned")
    mis = measure_misalignment(world, control_arm(world), ev, prior_label="baseline", fit_cases=train)
    lift = base.mean_regret - learned.mean_regret
    return WorldLearnProfile(
        world_id=world.world_id, evidence_floor_regret=mis.evidence_floor_regret,
        baseline_regret=base.mean_regret, learned_regret=learned.mean_regret, baseline_misalignment=mis,
        deterministic_learn_lift=lift,
        deterministic_learn_lift_share=(lift / base.mean_regret) if base.mean_regret > 0 else 0.0,
        live_model=live_model)
