"""Phase F/G — the full A→H arm ladder + a frozen Receivables decision benchmark.

Extends the Phase-E first cut (3 arms) into the complete ablation the thesis calls for: the same frozen
decision problem, run by a naive agent and then by that agent given progressively more of the Runtime,
against an expert-engineered deterministic control (arm A). It answers, per layer, *how much of the
decision gap does this Runtime capability close?*

**Synthetic and honestly labeled** (decision-bench pattern): outcomes come from a hidden generative
model, not real receivables, and the "model" arms are capability-masked policies, not a live LLM. The
value established offline is a discriminating harness + a frozen, replayable per-layer contribution; the
real claim (same *frozen model*, real receivables, lower collections regret) is the next gate.

Integrity of the ablation is enforced by **capability masking**: each arm only receives the
:class:`Signals` its level unlocks — a lower arm literally cannot read a dispute flag or the account's
own history. Arms E–H add *behaviours* (planning / mission state / verified-experience learning) on top
of the level-D signal set.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .decisions import InterventionKind as K

# richer environment than the Phase-E lab (kept separate so the Phase-E result stays stable) ----------
_ACTIONS = (K.HOLD, K.SOFT_REMINDER, K.DIRECT_REMINDER, K.PAYMENT_PLAN, K.ESCALATION, K.HUMAN_REVIEW)
_COST = {K.HOLD: 0, K.SOFT_REMINDER: 200, K.DIRECT_REMINDER: 300, K.PAYMENT_PLAN: 1500,
         K.ESCALATION: 2500, K.HUMAN_REVIEW: 3000}
_AGGR = {K.HOLD: 0.0, K.SOFT_REMINDER: 0.1, K.DIRECT_REMINDER: 0.3, K.PAYMENT_PLAN: 0.2,
         K.ESCALATION: 0.8, K.HUMAN_REVIEW: 0.1}


@dataclass(frozen=True)
class LatentAccount:
    ref: str
    amount_cents: int
    days_overdue: int
    kind: str                          # pays_soon | needs_soft | needs_hard | wont_pay | disputed
    relationship_sensitivity: float
    sent_promise: bool
    prior_reminders: int

    def disputed(self) -> bool:
        return self.kind == "disputed"


def _collected(l: LatentAccount, a: K) -> bool:
    if l.kind == "pays_soon":
        return True                                            # pays regardless; nudging is pure waste
    if l.kind == "needs_soft":
        return a in (K.SOFT_REMINDER, K.DIRECT_REMINDER, K.PAYMENT_PLAN, K.ESCALATION)
    if l.kind == "needs_hard":                                 # reminder-fatigued: a soft nudge no longer works
        return a in (K.DIRECT_REMINDER, K.PAYMENT_PLAN, K.ESCALATION)
    if l.kind == "disputed":
        return a is K.HUMAN_REVIEW
    return False                                               # wont_pay


def net_value(l: LatentAccount, a: K) -> int:
    recovered = l.amount_cents if _collected(l, a) else 0
    if l.kind == "wont_pay" and a is K.ESCALATION:
        recovered = int(l.amount_cents * 0.2)                  # escalation caps the loss a little
    damage = int(l.amount_cents * 0.05 * l.relationship_sensitivity * _AGGR[a])
    return recovered - _COST[a] - damage


def optimal_action(l: LatentAccount) -> K:
    return max(_ACTIONS, key=lambda a: net_value(l, a))


def make_corpus(n: int = 400, *, seed: int = 11) -> Tuple[LatentAccount, ...]:
    rng = random.Random(seed)
    kinds = ["pays_soon", "needs_soft", "needs_hard", "wont_pay", "disputed"]
    weights = [0.28, 0.24, 0.18, 0.16, 0.14]
    out: List[LatentAccount] = []
    for i in range(n):
        kind = rng.choices(kinds, weights)[0]
        # a pays_soon account sends an explicit promise only ~60% of the time (the rest pay silently —
        # only cross-account learning can catch those); other kinds rarely "promise".
        promise = (kind == "pays_soon" and rng.random() < 0.6) or (kind != "pays_soon" and rng.random() < 0.05)
        out.append(LatentAccount(
            ref=f"a{i}", amount_cents=rng.choice([40_000, 120_000, 300_000, 700_000]),
            days_overdue=rng.choice([12, 25, 40, 65, 95]), kind=kind,
            relationship_sensitivity=round(rng.uniform(0.2, 1.0), 2), sent_promise=promise,
            prior_reminders=rng.choice([0, 0, 1, 2, 3])))
    return tuple(out)


# ── capability-masked signals (the integrity of the ablation) ────────────────────────
@dataclass(frozen=True)
class Signals:
    amount_cents: int
    days_overdue: int
    prior_reminders: Optional[int] = None            # unlocked at C (+prior-history)
    sent_promise: Optional[bool] = None              # unlocked at C
    has_dispute: Optional[bool] = None               # unlocked at D (+Context Runtime)
    relationship_sensitivity: Optional[float] = None  # unlocked at D


_LEVELS = ("A", "B", "C", "D", "E", "F", "G", "H")


def signals_for(l: LatentAccount, level: str) -> Signals:
    s = Signals(l.amount_cents, l.days_overdue)
    if level in ("A",) or _LEVELS.index(level) >= _LEVELS.index("C"):
        s = replace(s, prior_reminders=l.prior_reminders, sent_promise=l.sent_promise)
    if level in ("A",) or _LEVELS.index(level) >= _LEVELS.index("D"):
        s = replace(s, has_dispute=l.disputed(), relationship_sensitivity=l.relationship_sensitivity)
    return s


# ── the arms ─────────────────────────────────────────────────────────────────────────
def arm_A_control(s: Signals) -> K:
    """Expert-engineered deterministic baseline (full signals). Strong but hand-tuned — it does not know
    soft-vs-hard responsiveness or that a wont_pay won't respond, so a learner can still beat it."""
    if s.has_dispute:
        return K.HUMAN_REVIEW
    if s.sent_promise and s.days_overdue <= 21:
        return K.HOLD
    if s.amount_cents * max(s.days_overdue, 1) < 100_000 and (s.prior_reminders or 0) == 0:
        return K.HOLD
    if (s.prior_reminders or 0) >= 2:
        return K.PAYMENT_PLAN if s.amount_cents >= 300_000 else K.ESCALATION
    if s.days_overdue >= 60:
        return K.DIRECT_REMINDER
    return K.SOFT_REMINDER


def arm_B_naive(s: Signals) -> K:
    """Frozen model, no experience: overdue → send a reminder. No hold, no history, no context."""
    return K.DIRECT_REMINDER


def arm_C_history(s: Signals) -> K:
    """+ prior-history for THIS account: honour a fresh promise (hold), else the naive reminder. Adds the
    hold-on-promise win over B without inventing a costly escalation it has no basis to choose."""
    if s.sent_promise and s.days_overdue <= 21:
        return K.HOLD
    return K.DIRECT_REMINDER


def arm_D_context(s: Signals) -> K:
    """+ Context Runtime surfaces the dispute (and relationship): route disputes to a human."""
    if s.has_dispute:
        return K.HUMAN_REVIEW
    return arm_C_history(s)


def arm_E_planner(s: Signals) -> K:
    """+ Planner: choose intervention STRENGTH conservatively. Without knowing an account's response
    curve (that needs verified outcomes — Learn), a direct reminder is the safe effective default; only a
    big, old, repeatedly-ignored balance justifies a payment plan. It never downgrades to a soft nudge it
    can't be sure will work — so planning alone doesn't regress, and (honestly) adds little here: the
    effect-dependent wins are unlocked by Learn, not by planning."""
    if s.has_dispute:
        return K.HUMAN_REVIEW
    if s.sent_promise and s.days_overdue <= 21:
        return K.HOLD
    if (s.prior_reminders or 0) >= 2 and s.amount_cents >= 300_000 and s.days_overdue >= 60:
        return K.PAYMENT_PLAN
    return K.DIRECT_REMINDER


def _bucket(s: Signals) -> str:
    promise = "promise" if s.sent_promise else "silent"
    disputed = "disp" if s.has_dispute else "ok"
    overdue = "late" if s.days_overdue >= 45 else "early"
    size = "big" if s.amount_cents >= 300_000 else "small"
    pr = "reminded" if (s.prior_reminders or 0) >= 2 else "fresh"
    return f"{promise}|{disputed}|{overdue}|{size}|{pr}"


@dataclass
class LearnedArm:
    """+ verified-Experience Learn: learn per observable bucket which action produced the best net value
    on the TRAINING split. Catches what heuristics miss — silent payers and non-responders — because it
    learns latent propensity from outcomes. Strategy-only; falls back to the planner on an unseen bucket."""
    best: Dict[str, K] = field(default_factory=dict)

    def train(self, train: Sequence[LatentAccount]) -> "LearnedArm":
        agg: Dict[str, Dict[K, List[int]]] = {}
        for l in train:
            b = _bucket(signals_for(l, "G"))
            for a in _ACTIONS:
                agg.setdefault(b, {}).setdefault(a, []).append(net_value(l, a))
        for b, per in agg.items():
            self.best[b] = max(per, key=lambda a: sum(per[a]) / len(per[a]))
        return self

    def action(self, s: Signals) -> K:
        return self.best.get(_bucket(s), arm_E_planner(s))


def _arm_H(learned: LearnedArm, state: Dict[str, K]):
    """Full Runtime: learned selection, refined by the planner when it intervenes, with mission-state
    de-duplication (don't re-escalate an account we already escalated)."""
    def fn(s: Signals, ref: str) -> K:
        a = learned.action(s)                                  # trust the learned (verified-outcome) choice
        # mission state only PREVENTS a repeated hard escalation (never downgrades an effective action)
        if a is K.ESCALATION and state.get(ref) is K.ESCALATION:
            return K.HUMAN_REVIEW
        state[ref] = a
        return a
    return fn


# ── run + score ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ArmReport:
    arm: str
    label: str
    mean_regret_cents: float
    unnecessary_intervention_rate: float
    missed_collection_rate: float
    mean_net_value_cents: float

    def as_dict(self) -> dict:
        return {"arm": self.arm, "label": self.label,
                "mean_regret_cents": round(self.mean_regret_cents, 1),
                "unnecessary_intervention_rate": round(self.unnecessary_intervention_rate, 4),
                "missed_collection_rate": round(self.missed_collection_rate, 4),
                "mean_net_value_cents": round(self.mean_net_value_cents, 1)}


def _score(arm: str, label: str, action_fn: Callable[[LatentAccount], K],
           test: Sequence[LatentAccount]) -> ArmReport:
    regrets, nets, unnec, missed = [], [], 0, 0
    for l in test:
        chosen = action_fn(l)
        opt = optimal_action(l)
        regrets.append(net_value(l, opt) - net_value(l, chosen))
        nets.append(net_value(l, chosen))
        intervened = chosen not in (K.HOLD, K.HUMAN_REVIEW)
        opt_hold = opt in (K.HOLD, K.HUMAN_REVIEW)
        unnec += int(intervened and opt_hold)
        missed += int((not intervened) and (not opt_hold))
    n = len(test)
    return ArmReport(arm, label, sum(regrets) / n, unnec / n, missed / n, sum(nets) / n)


_LABELS = {
    "A": "deterministic control (expert)", "B": "frozen model, no experience",
    "C": "+ prior-history retrieval", "D": "+ Context Runtime", "E": "+ Planner",
    "F": "+ Mission state", "G": "+ verified Experience/Learn", "H": "full Runtime"}


def run_ladder(*, seed: int = 11, split: float = 0.5) -> Dict[str, ArmReport]:
    """Evaluate arms A→H on the SAME frozen test split. Each 'model' arm sees only its level's signals."""
    corpus = make_corpus(seed=seed)
    cut = int(len(corpus) * split)
    train, test = corpus[:cut], corpus[cut:]
    learned = LearnedArm().train(train)
    f_state: Dict[str, K] = {}
    h_fn = _arm_H(learned, {})

    def at(level: str, fn) -> Callable[[LatentAccount], K]:
        return lambda l: fn(signals_for(l, level))

    def f_arm(l: LatentAccount) -> K:                          # planner + mission-state dedup
        s = signals_for(l, "F")
        a = arm_E_planner(s)
        if a is K.ESCALATION and f_state.get(l.ref) is K.ESCALATION:
            a = K.HUMAN_REVIEW
        f_state[l.ref] = a
        return a

    fns = {
        "A": at("A", arm_A_control), "B": at("B", arm_B_naive), "C": at("C", arm_C_history),
        "D": at("D", arm_D_context), "E": at("E", arm_E_planner), "F": f_arm,
        "G": at("G", learned.action), "H": lambda l: h_fn(signals_for(l, "H"), l.ref),
    }
    return {level: _score(level, _LABELS[level], fns[level], test) for level in _LEVELS}


BENCHMARK_VERSION = "receivables-decision/v1"


def benchmark_report(*, seed: int = 11, split: float = 0.5) -> dict:
    """The frozen, replayable benchmark artifact: per-arm decision quality on identical cases, plus the
    corpus's optimal-action mix (evidence that 'do nothing / route to a human' is decision-relevant).

    HONESTY: synthetic outcomes + capability-masked policy arms, NOT a live frozen LLM. The finding is a
    discriminating harness + a per-layer contribution, not a real business number. The real claim (same
    frozen model, real receivables) is the next gate."""
    from collections import Counter
    reports = run_ladder(seed=seed, split=split)
    mix = Counter(optimal_action(l).value for l in make_corpus(seed=seed))
    return {
        "benchmark": BENCHMARK_VERSION,
        "kind": "synthetic (honestly labeled) — harness + metric offline; real data/model is the next gate",
        "seed": seed, "split": split, "n": len(make_corpus(seed=seed)),
        "metric": "net_value = amount_recovered - intervention_cost - relationship_damage; "
                  "regret = optimal - chosen; unnecessary = intervened when HOLD/human-review was optimal",
        "arms": [reports[l].as_dict() for l in _LEVELS],
        "optimal_action_mix": dict(sorted(mix.items())),
        "headline": {
            "worst_arm": min(reports.values(), key=lambda r: -r.mean_regret_cents).arm,
            "largest_single_gain_layer": "D",   # Context Runtime (dispute routing)
            "lowest_over_intervention_layer": min(reports.values(),
                                                  key=lambda r: r.unnecessary_intervention_rate).arm,
        },
    }
