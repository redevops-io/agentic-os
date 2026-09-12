"""Candidate learned policy + explicit promotion gate (PR-sequence.odt PR 7 / §8).

PR6 measured the runtime's recommendations against the human's actions in Shadow. PR7 is the first step
that CHANGES the active policy — but under the discipline the odt insists on (§8):

    collect continuously  →  evaluate periodically  →  promote policy EXPLICITLY

not ``outcome → immediately changes policy``. So this module never mutates the live policy per outcome.
It (a) builds a *candidate* :class:`~agentic_os.outcome_learning.UtilityModel` from the durable outcome
log, (b) evaluates it against the *active* policy on a **held-out time split**, and (c) offers a gated,
discrete promotion that an operator (or a periodic cycle) enacts as one auditable step.

What a PASSIVE outcome log honestly supports
--------------------------------------------
A passive log records the reward of the action that was ACTUALLY taken — never the counterfactual reward
of the actions that weren't. So we do **not** fabricate off-policy "lift". The honest, computable
question on held-out data is: *does the learned per-key value estimate predict future realized rewards
better than the static prior?* We score exactly that (held-out mean squared error of the value estimate
vs realized reward). A policy whose learned structure doesn't generalize scores no better than the
pooled prior — and the gate then refuses to promote it. That refusal is the point.

Honesty / guarantees
  * **Time split by arrival order** — OutcomeEvents carry no wall-clock; the durable store loads in
    insertion order, which IS the order outcomes were observed. Earliest ``1-holdout`` = train, latest
    = held-out test. No shuffling: the future never trains the past.
  * **Replayable & provenanced** — everything is a pure function of the ordered log; ``train_digest``
    is a stable content hash of the training slice (§7 reproducibility), so the same log yields the same
    candidate and the same decision.
  * **Governance-preserving** — a promoted policy is still only a ``utility_fn``; every selection it
    influences still routes through ``decide`` (risk tier → approval). Promotion changes WHICH action is
    favoured, never whether a human gate applies.
  * **Deploy-on-all / evaluate-on-holdout** — the candidate that gets promoted is trained on the FULL
    log (standard); the candidate that gets *scored* is trained only on the train slice (no leakage).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Sequence, Tuple

from agentic_os.outcome_learning import UtilityModel
from agentic_os.priority_engine import OutcomeEvent, OutcomeLog, UtilityFn

Key = Tuple[str, str]


def dataset_digest(events: Sequence[OutcomeEvent], reward_weights: Optional[Mapping[str, float]] = None) -> str:
    """Stable content hash over the ordered log — provenance for reproducibility (odt §7). Two runs over
    the same outcomes in the same order produce the same digest (and therefore the same candidate)."""
    rw = dict(reward_weights or {})
    h = hashlib.sha256()
    for e in events:
        h.update(json.dumps([e.source_app, e.action_kind, round(e.scalar_reward(rw), 6),
                             round(e.attribution_confidence, 6)], sort_keys=True).encode())
    return h.hexdigest()[:16]


def time_split(events: Sequence[OutcomeEvent], *, holdout_fraction: float = 0.3
               ) -> Tuple[List[OutcomeEvent], List[OutcomeEvent]]:
    """Split the log by ARRIVAL ORDER into (train, held-out). The future never trains the past."""
    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be in (0,1)")
    evs = list(events)
    n = len(evs)
    if n < 2:
        return evs, []
    k = int(round(n * (1.0 - holdout_fraction)))
    k = max(1, min(k, n - 1))                    # both slices non-empty when n>=2
    return evs[:k], evs[k:]


@dataclass(frozen=True)
class EvaluationResult:
    """Held-out comparison of the candidate (learned) vs the active (static prior) policy."""
    n_train: int
    n_test: int
    static_mse: float             # pooled-prior value estimate vs realized held-out reward
    learned_mse: float            # learned per-key value estimate vs realized held-out reward
    keys_covered: int             # test keys the candidate actually had training evidence for
    train_digest: str

    @property
    def improvement(self) -> float:
        """Absolute reduction in held-out squared error (>0 ⇒ learned predicts the future better)."""
        return self.static_mse - self.learned_mse

    @property
    def relative_improvement(self) -> float:
        return (self.improvement / self.static_mse) if self.static_mse > 0 else 0.0


def evaluate_candidate(events: Sequence[OutcomeEvent], *, holdout_fraction: float = 0.3,
                       prior_weight: float = 5.0,
                       reward_weights: Optional[Mapping[str, float]] = None) -> EvaluationResult:
    """Time-split the log, fit the candidate on the train slice ONLY, and score both policies on the
    held-out slice by how well each predicts realized reward. The static baseline is the pooled train
    mean (no learned per-key structure); the candidate is the shrinkage-blended per-key estimate."""
    rw = dict(reward_weights or {})
    train, test = time_split(events, holdout_fraction=holdout_fraction)
    pooled = (sum(e.scalar_reward(rw) for e in train) / len(train)) if train else 0.0
    model = UtilityModel(prior_weight=prior_weight, reward_weights=rw).fit(OutcomeLog(events=list(train)))
    s_se = l_se = 0.0
    covered = set()
    for e in test:
        realized = e.scalar_reward(rw)
        key: Key = (e.source_app, e.action_kind)
        if model.observed(key) is not None:
            covered.add(key)
        s_se += (pooled - realized) ** 2
        l_se += (model.predict_key(key, pooled) - realized) ** 2
    n = len(test) or 1
    return EvaluationResult(n_train=len(train), n_test=len(test), static_mse=s_se / n,
                            learned_mse=l_se / n, keys_covered=len(covered),
                            train_digest=dataset_digest(train, rw))


@dataclass(frozen=True)
class PromotionDecision:
    promote: bool
    reasons: Tuple[str, ...]      # why NOT (when refused), or the one confirming reason (when promoted)


@dataclass(frozen=True)
class PromotionGate:
    """The explicit gate (odt §8). All conditions must hold for a candidate to be promotable; the
    defaults are conservative — enough held-out data, enough covered keys, and a real held-out win."""
    min_test_events: int = 30
    min_keys_covered: int = 2
    min_relative_improvement: float = 0.05        # candidate must cut held-out error by >= 5%

    def decide(self, ev: EvaluationResult) -> PromotionDecision:
        reasons: List[str] = []
        if ev.n_test < self.min_test_events:
            reasons.append(f"insufficient held-out data ({ev.n_test} < {self.min_test_events})")
        if ev.keys_covered < self.min_keys_covered:
            reasons.append(f"too few keys with training evidence ({ev.keys_covered} < {self.min_keys_covered})")
        if ev.relative_improvement < self.min_relative_improvement:
            reasons.append(f"held-out improvement {ev.relative_improvement:.1%} "
                           f"< required {self.min_relative_improvement:.1%}")
        if reasons:
            return PromotionDecision(False, tuple(reasons))
        return PromotionDecision(True, (f"candidate cuts held-out error by {ev.relative_improvement:.1%} "
                                        f"over {ev.n_test} outcomes / {ev.keys_covered} keys",))


@dataclass(frozen=True)
class PolicyVersion:
    """A named policy the loop can select under. ``static`` carries no model (the producer prior);
    ``learned`` carries a fitted :class:`UtilityModel`. Digest + n_train pin its provenance."""
    version: str
    kind: str                                     # "static" | "learned"
    model: Optional[UtilityModel] = None
    train_digest: str = ""
    n_train: int = 0

    def utility_fn(self) -> Optional[UtilityFn]:
        """The ``utility_fn`` for ``select_action`` — None for the static policy (uses the prior)."""
        return self.model.as_utility_fn() if self.model is not None else None


#: the policy every deployment starts on — the producer prior, no learning.
STATIC_POLICY = PolicyVersion(version="static-0", kind="static")


@dataclass
class PolicyRegistry:
    """Holds the single ACTIVE policy plus the promotion history. Promotion is an explicit, discrete
    call (never a side effect of observing an outcome), so the active policy only ever changes at an
    auditable boundary."""
    active: PolicyVersion = field(default_factory=lambda: STATIC_POLICY)
    history: List[PolicyVersion] = field(default_factory=list)

    def promote(self, candidate: PolicyVersion) -> PolicyVersion:
        self.history.append(self.active)
        self.active = candidate
        return self.active


def build_candidate(events: Sequence[OutcomeEvent], *, prior_weight: float = 5.0,
                    reward_weights: Optional[Mapping[str, float]] = None,
                    version: Optional[str] = None) -> PolicyVersion:
    """The candidate to DEPLOY — trained on the FULL log (evaluation trains on the train slice only)."""
    rw = dict(reward_weights or {})
    model = UtilityModel(prior_weight=prior_weight, reward_weights=rw).fit(OutcomeLog(events=list(events)))
    dig = dataset_digest(events, rw)
    return PolicyVersion(version=version or f"learned-{dig}", kind="learned", model=model,
                         train_digest=dig, n_train=len(events))


@dataclass(frozen=True)
class PromotionReport:
    result: EvaluationResult
    decision: PromotionDecision
    candidate: PolicyVersion
    active: PolicyVersion
    promoted: bool


def run_promotion_cycle(outcome_store, *, gate: Optional[PromotionGate] = None,
                        registry: Optional[PolicyRegistry] = None, holdout_fraction: float = 0.3,
                        prior_weight: float = 5.0,
                        reward_weights: Optional[Mapping[str, float]] = None,
                        apply: bool = False) -> PromotionReport:
    """One EXPLICIT collect → evaluate → promote cycle (odt §8), NOT online-per-outcome. Loads the
    durable log, time-splits it, scores candidate-vs-active on the held-out slice, and returns the
    gate's decision. The active policy is mutated ONLY when ``apply=True`` AND the gate passes — so
    promotion stays a discrete, gated, auditable step even when a periodic job runs the cycle."""
    gate = gate or PromotionGate()
    registry = registry or PolicyRegistry()
    events = outcome_store.load()
    result = evaluate_candidate(events, holdout_fraction=holdout_fraction, prior_weight=prior_weight,
                                reward_weights=reward_weights)
    decision = gate.decide(result)
    candidate = build_candidate(events, prior_weight=prior_weight, reward_weights=reward_weights)
    promoted = False
    if apply and decision.promote:
        registry.promote(candidate)
        promoted = True
    return PromotionReport(result=result, decision=decision, candidate=candidate,
                           active=registry.active, promoted=promoted)
