"""Phase 0 — frozen shared benchmark contracts + the general §6 transition kernel + evaluator.

These are the pieces every world in the Simulation Lab shares, so a result on one world is comparable to a
result on another and no world can quietly cheat:

  * :class:`DecisionCase`   — one held-out decision. ``observable`` is *exactly* what an arm sees;
                              ``latent`` is oracle-only ground truth (never passed to an arm); ``known_at``
                              is the bitemporal cut so an evaluator can prove no future evidence leaked.
  * :func:`apply_kernel`    — the general §6 state-transition kernel: the SAME three invariants the
                              receivables ``transitions`` module enforces, over any world's state. A
                              rejected / inadmissible / failed action never executes, so it never evolves
                              state and never becomes training Experience.
  * :func:`evaluate`        — scores an arm against the world's hidden oracle: per-case regret vs the
                              per-case optimal action, plus the intervention-quality rates the reframe
                              demands (did we act when doing nothing was optimal, and vice versa).
  * arms                    — ``control_arm`` (the world's deterministic rule baseline, arm A) and
                              ``learned_arm`` (retrieve the best VERIFIED action per observable bucket from
                              acquired Experience, arms G/H). The frozen-model arm lives in ``model_arm``.

The metric follows the reframe that governed the Receivables track: the product is NOT a materiality/risk
score, it is the decision *should we intervene, and how*; "do nothing" is a first-class candidate, and an
arm that just intervenes more is penalised through the unnecessary-intervention rate, not rewarded.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, runtime_checkable

from ..receipts import VerificationState
from ..transitions import Admissibility

BENCHMARK_VERSION = "business-sim-lab/v1"


def digest(obj: Any) -> str:
    """Stable content digest over any JSON-able structure (canonical key order)."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


# --------------------------------------------------------------------------- Phase 0 contracts
@dataclass(frozen=True)
class DecisionCase:
    """One held-out decision point in a world.

    ``observable`` is the ONLY thing an arm is allowed to read. ``latent`` is the hidden ground truth used
    exclusively by the oracle to score outcomes — leaking any latent key into ``observable`` is a benchmark
    defect the mutation suite checks for.
    """
    case_id: str
    world_id: str
    observable: Mapping[str, Any]
    latent: Mapping[str, Any]
    known_at: int = 0

    def observable_digest(self) -> str:
        return digest(dict(self.observable))


@dataclass(frozen=True)
class Proposal:
    """An arm's decision for one case: the chosen action + an optional rationale."""
    action: str
    rationale: str = ""


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    chosen: str
    effective: str          # the action that actually took effect (inadmissible actions fall back to HOLD)
    optimal: str
    net_value: float        # oracle net value of the effective action
    optimal_value: float    # oracle net value of the per-case optimal action
    regret: float           # max(0, optimal_value - net_value)
    admissibility: Admissibility
    executed: bool
    verification_state: VerificationState
    intervened: bool
    optimal_was_hold: bool


@dataclass(frozen=True)
class ArmReport:
    arm: str
    world_id: str
    n: int
    mean_regret: float
    total_regret: float
    mean_net_value: float
    unnecessary_intervention_rate: float   # acted when the optimal action was a HOLD-class action
    missed_intervention_rate: float        # held when the optimal action was to intervene
    accuracy: float                        # fraction whose effective action == per-case optimal
    extra: Mapping[str, float] = field(default_factory=dict)  # world-declared operational metrics

    def summary(self) -> str:
        return (f"{self.arm:<22} regret={self.mean_regret:9.1f} acc={self.accuracy:5.2f} "
                f"over-interv={self.unnecessary_intervention_rate:5.2f} "
                f"missed={self.missed_intervention_rate:5.2f}")


# --------------------------------------------------------------------------- the World interface (§5)
@runtime_checkable
class World(Protocol):
    """A business decision world. Provides held-out cases, a hidden oracle, feasibility, and the seams the
    arms need. The oracle (``net_value`` / ``optimal``) reads ``latent``; everything an arm calls reads only
    ``observable``."""

    world_id: str

    def actions(self) -> tuple[str, ...]: ...
    def hold_actions(self) -> frozenset[str]: ...           # the "no active intervention" set
    def default_hold(self) -> str: ...                      # fallback when a chosen action is inadmissible
    def cases(self, *, seed: int, n: int, shift: float = 0.0) -> tuple[DecisionCase, ...]: ...
    def net_value(self, latent: Mapping[str, Any], action: str) -> float: ...   # oracle
    def optimal(self, latent: Mapping[str, Any]) -> str: ...                    # oracle
    def admissible_action(self, observable: Mapping[str, Any], action: str) -> bool: ...
    def bucket(self, observable: Mapping[str, Any]) -> str: ...   # coarse observable key, for learning
    def baseline_action(self, observable: Mapping[str, Any]) -> str: ...        # arm A rule
    def extra_metrics(self, outcomes: Sequence[CaseOutcome],
                      cases: Sequence[DecisionCase]) -> Mapping[str, float]: ...


# --------------------------------------------------------------------------- §6 general transition kernel
@dataclass(frozen=True)
class KernelResult:
    admissibility: Admissibility
    admitted: bool
    executed: bool               # a real, VERIFIED-eligible state-changing execution occurred
    verification_state: VerificationState
    outcome_pending: bool        # executed but the business outcome is only observable later


def apply_kernel(*, state_valid: bool, action_admissible: bool, approved: bool,
                 provider_ok: bool, reconciled: Optional[bool]) -> KernelResult:
    """The general §6 kernel — identical invariants to ``..transitions.apply_transition`` over any world.

    * INITIAL_STATE_ADMISSIBILITY — an invalid state admits nothing.
    * ACTION_ADMISSIBILITY        — an infeasible action is never executed.
    * TRAJECTORY_CONTRACT_CONSISTENCY — only an approved, admissible, provider-successful, reconciled action
      counts as executed+VERIFIED; anything else leaves ``executed=False`` so it can neither evolve state
      nor become Experience (§2.3: you cannot learn "verification works" from a step-up you never ran).
    """
    if not state_valid:
        return KernelResult(Admissibility.INADMISSIBLE_STATE, False, False,
                            VerificationState.NOT_APPLICABLE, False)
    if not action_admissible:
        return KernelResult(Admissibility.INADMISSIBLE_ACTION, False, False,
                            VerificationState.NOT_APPLICABLE, False)
    if not approved:
        return KernelResult(Admissibility.REJECTED, False, False, VerificationState.NOT_APPLICABLE, False)
    if not provider_ok:                                   # execution attempted, provider failed
        return KernelResult(Admissibility.ADMITTED, True, False, VerificationState.REFUTED, False)
    if reconciled is True:
        return KernelResult(Admissibility.ADMITTED, True, True, VerificationState.VERIFIED, False)
    if reconciled is None:                                # provider ok, outcome not yet observable
        return KernelResult(Admissibility.ADMITTED, True, True, VerificationState.UNKNOWN, True)
    return KernelResult(Admissibility.ADMITTED, True, False, VerificationState.REFUTED, False)


# --------------------------------------------------------------------------- evaluator (§5 score)
def evaluate(world: World, arm: Callable[[DecisionCase], Proposal],
             cases: Sequence[DecisionCase], *, arm_name: str) -> ArmReport:
    """Score an arm against the world's hidden oracle.

    An inadmissible chosen action cannot take effect — it falls back to the world's default HOLD (you cannot
    execute a step-up you have no capacity for), and the outcome is scored on that effective action. Regret
    is measured against the per-case optimal, so choosing interventions more cleverly does not help unless it
    was actually the right thing to do; the intervention-quality rates keep "do nothing" first-class.
    """
    outcomes: list[CaseOutcome] = []
    holds = world.hold_actions()
    for c in cases:
        chosen = arm(c).action
        admissible = world.admissible_action(c.observable, chosen)
        effective = chosen if admissible else world.default_hold()
        nv = world.net_value(c.latent, effective)
        opt = world.optimal(c.latent)
        ov = world.net_value(c.latent, opt)
        kr = apply_kernel(
            state_valid=True, action_admissible=admissible,
            approved=effective not in holds,               # a HOLD needs no execution/approval
            provider_ok=True, reconciled=True)
        outcomes.append(CaseOutcome(
            case_id=c.case_id, chosen=chosen, effective=effective, optimal=opt,
            net_value=nv, optimal_value=ov, regret=max(0.0, ov - nv),
            admissibility=kr.admissibility, executed=kr.executed,
            verification_state=kr.verification_state,
            intervened=effective not in holds, optimal_was_hold=opt in holds))

    n = len(outcomes) or 1
    regrets = [o.regret for o in outcomes]
    unnecessary = sum(1 for o in outcomes if o.intervened and o.optimal_was_hold)
    hold_opt = sum(1 for o in outcomes if o.optimal_was_hold) or 1
    missed = sum(1 for o in outcomes if not o.intervened and not o.optimal_was_hold)
    interv_opt = sum(1 for o in outcomes if not o.optimal_was_hold) or 1
    return ArmReport(
        arm=arm_name, world_id=world.world_id, n=len(outcomes),
        mean_regret=statistics.mean(regrets), total_regret=sum(regrets),
        mean_net_value=statistics.mean([o.net_value for o in outcomes]),
        unnecessary_intervention_rate=unnecessary / hold_opt,
        missed_intervention_rate=missed / interv_opt,
        accuracy=sum(1 for o in outcomes if o.effective == o.optimal) / n,
        extra=dict(world.extra_metrics(outcomes, cases)))


# --------------------------------------------------------------------------- arms A + G/H
def control_arm(world: World) -> Callable[[DecisionCase], Proposal]:
    """Arm A — the world's deterministic rule baseline. No history, no learning."""
    def arm(c: DecisionCase) -> Proposal:
        return Proposal(world.baseline_action(c.observable), rationale="baseline rule")
    return arm


def _acquire_experience(world: World, train: Sequence[DecisionCase]) -> dict[str, str]:
    """Learn, per observable bucket, the action with the best *verified* mean net value on training cases.

    This is the honest learning loop: for each bucket we try each feasible action, run it through the §6
    kernel, and keep only executed+VERIFIED outcomes as Experience (a HOLD is always eligible — doing
    nothing is verifiable). The oracle scores the outcome; the learner sees only the bucket + verified
    result, never the latent. The chosen policy is the argmax-verified action per bucket.
    """
    from collections import defaultdict
    tally: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    holds = world.hold_actions()
    for c in train:
        b = world.bucket(c.observable)
        for a in world.actions():
            if not world.admissible_action(c.observable, a):
                continue
            kr = apply_kernel(state_valid=True, action_admissible=True,
                              approved=a not in holds, provider_ok=True, reconciled=True)
            if not (kr.executed or a in holds):            # only verified executions (or a verifiable hold)
                continue
            tally[b][a].append(world.net_value(c.latent, a))
    policy: dict[str, str] = {}
    for b, per_action in tally.items():
        policy[b] = max(per_action, key=lambda a: statistics.mean(per_action[a]))
    return policy


def learned_arm(world: World, train: Sequence[DecisionCase]) -> Callable[[DecisionCase], Proposal]:
    """Arms G/H — decide from retrieved verified Experience; fall back to the baseline for unseen buckets."""
    policy = _acquire_experience(world, train)
    def arm(c: DecisionCase) -> Proposal:
        b = world.bucket(c.observable)
        if b in policy:
            return Proposal(policy[b], rationale=f"verified experience for bucket {b}")
        return Proposal(world.baseline_action(c.observable), rationale="no experience; baseline")
    return arm


# --------------------------------------------------------------------------- run manifest (§33)
@dataclass(frozen=True)
class RunManifest:
    benchmark_version: str
    world_id: str
    n_eval: int
    seed: int
    shift: float
    corpus_digest: str
    reports: Mapping[str, ArmReport]
    model_identity: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "benchmark_version": self.benchmark_version, "world_id": self.world_id,
            "n_eval": self.n_eval, "seed": self.seed, "shift": self.shift,
            "corpus_digest": self.corpus_digest, "model_identity": self.model_identity,
            "arms": {name: {
                "mean_regret": round(r.mean_regret, 2), "total_regret": round(r.total_regret, 2),
                "mean_net_value": round(r.mean_net_value, 2), "accuracy": round(r.accuracy, 4),
                "unnecessary_intervention_rate": round(r.unnecessary_intervention_rate, 4),
                "missed_intervention_rate": round(r.missed_intervention_rate, 4),
                "extra": {k: round(v, 4) for k, v in r.extra.items()},
            } for name, r in self.reports.items()},
        }


def run_world(world: World, *, seed: int = 7, n_train: int = 400, n_eval: int = 200,
              shift: float = 0.0) -> RunManifest:
    """Run the deterministic arms (A control, G/H learned) on a world and return a manifest.

    Training and evaluation corpora are drawn with different sub-seeds so the learned arm is scored on
    held-out cases (anti-memorization). ``shift`` applies a covariate shift to the eval corpus only, so a
    learned policy that merely memorised the training mix is exposed (§29 Gate 5, transfer isolation).
    """
    train = world.cases(seed=seed, n=n_train)
    eval_cases = world.cases(seed=seed + 10_000, n=n_eval, shift=shift)
    reports = {
        "A_control": evaluate(world, control_arm(world), eval_cases, arm_name="A_control"),
        "GH_learned": evaluate(world, learned_arm(world, train), eval_cases, arm_name="GH_learned"),
    }
    return RunManifest(
        benchmark_version=BENCHMARK_VERSION, world_id=world.world_id, n_eval=len(eval_cases),
        seed=seed, shift=shift,
        corpus_digest=digest([c.observable_digest() for c in eval_cases]), reports=reports)
