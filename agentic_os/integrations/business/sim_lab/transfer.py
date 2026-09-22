"""Cross-domain transfer — Receivables → Stale-Quote, done honestly.

This is the experiment the reframe asks for. Four deterministic, reproducible arms at the *policy* level
(the frozen-model S0–S3 arms live in ``stale_quote_model_arm``), plus a negative control:

  * ``baseline``            — the activity-driven sales cadence (no learning).
  * ``native_learned``      — learn the best action per Stale-Quote observable bucket (S1: is there a local
                              learnable policy gap at all?).
  * ``zero_shot_transfer``  — apply the *abstract posture policy learned on Receivables* to Stale-Quote via
                              its posture→native bridge, using NO Stale-Quote outcomes (S2: does anything
                              transfer zero-shot?).
  * ``transfer_adapt``      — start from the transferred posture, override with native experience wherever
                              it exists (S3: does a transferred prior initialise adaptation usefully?).
  * ``literal_transfer``    — NEGATIVE CONTROL: carry Receivables' literal (value/days/touches → action)
                              thresholds across by name. If this does *well*, the two simulators are
                              suspiciously aligned and the transfer result is not meaningful.

Preregistered interpretation (before looking at numbers):
  S1 > baseline           → Stale-Quote has a learnable local policy gap.
  S2 > baseline           → genuine zero-shot cross-domain lesson transfer.
  S3 reaches low regret at smaller train sizes than S1 → transferred Experience is a useful initialisation.
  no lift                 → the frozen prior is adequate, or the learned structure does not transfer.
  negative transfer (S2 < baseline) → especially interesting: the lesson's scope is too broad; a rule
                          learned in collections is being misapplied where sales dynamics differ.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Callable, Sequence

from ..decisions import InterventionKind as K
from ..receivables_benchmark import make_corpus, optimal_action
from .abstract import (
    AbstractFeatures, Elapsed, Fatigue, Posture, PostureModel, Readiness, Stakes, elapsed_of, fatigue_of)
from .harness import ArmReport, DecisionCase, Proposal, control_arm, evaluate, learned_arm
from .stale_quote_world import (
    CALL, CLOSE_LOST, DIRECT_FOLLOWUP, HUMAN_REVIEW, OFFER_ALTERNATIVE, SOFT_FOLLOWUP, StaleQuoteWorld)

# Receivables action → abstract posture. Note there is NO Receivables action that maps to ELICIT or
# DISENGAGE: a transferred policy therefore can never recommend REQUEST_TIMELINE or CLOSE_LOST — the ceiling
# on how far collections experience can carry a sales decision.
_REC_ACTION_TO_POSTURE = {
    K.HOLD: Posture.HOLD, K.SOFT_REMINDER: Posture.GENTLE, K.DIRECT_REMINDER: Posture.DECISIVE,
    K.PAYMENT_PLAN: Posture.CONCESSION, K.ESCALATION: Posture.DECISIVE, K.HUMAN_REVIEW: Posture.HUMAN}


def _receivables_abstract(account) -> AbstractFeatures:
    """Map a Receivables account to the shared abstract features (the SOURCE domain's own view)."""
    readiness = (Readiness.POSITIVE if account.sent_promise
                 else Readiness.NEGATIVE if account.kind == "wont_pay" else Readiness.NEUTRAL)
    return AbstractFeatures(
        readiness=readiness, elapsed=elapsed_of(account.days_overdue),
        fatigue=fatigue_of(account.prior_reminders),
        stakes=Stakes.HIGH if account.amount_cents >= 300_000 else Stakes.LOW,
        contested=account.disputed())


def learn_receivables_posture_policy(*, n: int = 2000, seed: int = 11) -> PostureModel:
    """Derive the abstract posture policy from what actually works in Receivables — a *semantic* lesson
    (readiness/elapsed/fatigue/stakes → posture), not literal thresholds."""
    tally: dict[str, Counter] = defaultdict(Counter)
    for acct in make_corpus(n, seed=seed):
        feats = _receivables_abstract(acct)
        tally[feats.bucket()][_REC_ACTION_TO_POSTURE[optimal_action(acct)]] += 1
    policy = {b: counts.most_common(1)[0][0] for b, counts in tally.items()}
    overall = Counter(p for counts in tally.values() for p, c in counts.items() for _ in range(c))
    return PostureModel(policy=policy, default=overall.most_common(1)[0][0], source="receivables")


# ── arms ─────────────────────────────────────────────────────────────────────────────
def zero_shot_transfer_arm(world: StaleQuoteWorld, policy: PostureModel) -> Callable[[DecisionCase], Proposal]:
    def arm(c: DecisionCase) -> Proposal:
        feats = world.abstract_features(c.observable)
        posture = policy.posture_for(feats)
        return Proposal(world.action_for_posture(posture, c.observable),
                        rationale=f"transferred posture {posture.value} (from {policy.source})")
    return arm


def transfer_adapt_arm(world: StaleQuoteWorld, policy: PostureModel,
                       train: Sequence[DecisionCase]) -> Callable[[DecisionCase], Proposal]:
    """S3: transferred posture as the prior; native experience overrides it wherever the bucket was seen."""
    native = learned_arm(world, train)
    from .harness import _acquire_experience
    native_policy = _acquire_experience(world, train)
    transfer = zero_shot_transfer_arm(world, policy)
    def arm(c: DecisionCase) -> Proposal:
        if world.bucket(c.observable) in native_policy:
            return native(c)
        return transfer(c)
    return arm


def literal_transfer_arm(world: StaleQuoteWorld, *, n: int = 2000, seed: int = 11
                         ) -> Callable[[DecisionCase], Proposal]:
    """NEGATIVE CONTROL — carry Receivables' literal (value/days/touches) thresholds across by action name.
    Should do poorly; if it does well the simulators are suspiciously aligned."""
    name_map = {K.HOLD: "hold", K.SOFT_REMINDER: SOFT_FOLLOWUP, K.DIRECT_REMINDER: DIRECT_FOLLOWUP,
                K.PAYMENT_PLAN: OFFER_ALTERNATIVE, K.ESCALATION: CALL, K.HUMAN_REVIEW: HUMAN_REVIEW}

    def lit_key_rec(acct) -> str:
        vb = "h" if acct.amount_cents >= 300_000 else "l"
        db = "e" if acct.days_overdue < 30 else "m" if acct.days_overdue < 60 else "l"
        return f"{vb}:{db}:t{min(acct.prior_reminders,4)}"

    tally: dict[str, Counter] = defaultdict(Counter)
    for acct in make_corpus(n, seed=seed):
        tally[lit_key_rec(acct)][optimal_action(acct)] += 1
    lit_policy = {k: c.most_common(1)[0][0] for k, c in tally.items()}
    fallback = Counter(a for c in tally.values() for a, cnt in c.items() for _ in range(cnt)).most_common(1)[0][0]

    def lit_key_sq(observable) -> str:
        vb = "h" if observable.get("quote_value_band") in ("l", "xl") else "l"
        d = int(observable.get("days_since_quote", 0))
        db = "e" if d < 30 else "m" if d < 60 else "l"
        return f"{vb}:{db}:t{min(int(observable.get('prior_touches',0)),4)}"

    def arm(c: DecisionCase) -> Proposal:
        rec_action = lit_policy.get(lit_key_sq(c.observable), fallback)
        return Proposal(name_map[rec_action], rationale="literal receivables threshold (control)")
    return arm


# ── deterministic experiment + sample-efficiency curve ─────────────────────────────────
def run_transfer_experiment(*, seed: int = 7, n_train: int = 400, n_eval: int = 400,
                            shift: float = 0.0) -> dict[str, ArmReport]:
    world = StaleQuoteWorld()
    train = world.cases(seed=seed, n=n_train)
    eval_cases = world.cases(seed=seed + 10_000, n=n_eval, shift=shift)
    policy = learn_receivables_posture_policy()
    return {
        "baseline": evaluate(world, control_arm(world), eval_cases, arm_name="baseline"),
        "S1_native": evaluate(world, learned_arm(world, train), eval_cases, arm_name="S1_native"),
        "S2_zero_shot_transfer": evaluate(
            world, zero_shot_transfer_arm(world, policy), eval_cases, arm_name="S2_zero_shot_transfer"),
        "S3_transfer_adapt": evaluate(
            world, transfer_adapt_arm(world, policy, train), eval_cases, arm_name="S3_transfer_adapt"),
        "literal_transfer_control": evaluate(
            world, literal_transfer_arm(world), eval_cases, arm_name="literal_transfer_control"),
    }


def sample_efficiency_curve(*, seed: int = 7, n_eval: int = 400,
                            sizes: Sequence[int] = (10, 25, 50, 100, 200, 400)) -> dict[int, dict[str, float]]:
    """S3-vs-S1: mean regret of native-only vs transfer-initialised-then-adapt at each training size.
    Transfer is a useful initialisation iff S3 reaches low regret at smaller train sizes than S1."""
    world = StaleQuoteWorld()
    eval_cases = world.cases(seed=seed + 10_000, n=n_eval)
    policy = learn_receivables_posture_policy()
    out: dict[int, dict[str, float]] = {}
    for m in sizes:
        train = world.cases(seed=seed, n=m)
        s1 = evaluate(world, learned_arm(world, train), eval_cases, arm_name="S1")
        s3 = evaluate(world, transfer_adapt_arm(world, policy, train), eval_cases, arm_name="S3")
        out[m] = {"S1_native": round(s1.mean_regret, 1), "S3_transfer_adapt": round(s3.mean_regret, 1)}
    return out
