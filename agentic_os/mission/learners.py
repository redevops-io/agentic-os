"""P9b — three learners kept SEPARATE, behind a promotion ladder.

"Learning" here is three different problems with distinct feedback signals, safety risks and
rollout strategies — they must not share a model:

  * Routing learning        — which operator/provider executes a capability best.
  * Model-selection learning — which model tier fits a reasoning step (cost·latency·confidence·success).
  * Plan-policy learning     — which execution graph / ordering works best per mission type.

None mutates production behaviour directly. Every learner follows the ladder

    Observe → Recommend → Shadow-evaluate → Policy-constrained promotion

so unrestricted online mutation can't undermine the determinism P7 established. The *only* path that
changes production routing is `LearningStack.promote()`, which a policy (or a P10 governance action)
must call explicitly.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .types import new_id


@dataclass
class Recommendation:
    kind: str                  # routing | model_selection | plan_policy
    subject: str               # outcome / reasoning-step / mission-kind
    current: str               # the option production uses now
    recommended: str           # the option the evidence favours
    support: int               # observations backing the recommendation
    lift: float                # measured improvement over `current`
    status: str = "recommended"  # recommended → shadow → promoted
    id: str = field(default_factory=lambda: new_id("rec"))


class _StatTable:
    """Success rate + support per (subject, option)."""
    def __init__(self):
        self._s: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))

    def record(self, subject: str, option: str, ok: bool) -> None:
        self._s[subject][option].append(1 if ok else 0)

    def rate(self, subject: str, option: str) -> float | None:
        h = self._s[subject].get(option)
        return round(sum(h) / len(h), 3) if h else None

    def options(self, subject: str) -> dict[str, list[int]]:
        return dict(self._s[subject])

    def subjects(self) -> list[str]:
        return list(self._s)


class Recommender:
    """Observe outcomes; recommend a better option once enough evidence shows a real lift."""
    kind = "base"

    def __init__(self, min_support: int = 5, min_lift: float = 0.1):
        self.tbl = _StatTable()
        self.min_support = min_support
        self.min_lift = min_lift

    def observe(self, subject: str, option: str, ok: bool) -> None:
        self.tbl.record(subject, option, ok)

    def recommend(self) -> list[Recommendation]:
        recs: list[Recommendation] = []
        for subj in self.tbl.subjects():
            opts = self.tbl.options(subj)
            if len(opts) < 2:
                continue
            current = max(opts, key=lambda o: len(opts[o]))   # production = the most-used option
            cur_rate = self.tbl.rate(subj, current) or 0.0
            best = None
            for opt, hist in opts.items():
                if opt == current or len(hist) < self.min_support:
                    continue
                lift = round((self.tbl.rate(subj, opt) or 0.0) - cur_rate, 3)
                if lift >= self.min_lift and (best is None or lift > best.lift):
                    best = Recommendation(self.kind, subj, current, opt, len(hist), lift)
            if best is not None:
                recs.append(best)
        return recs


class RoutingLearner(Recommender):
    kind = "routing"            # subject = outcome, option = provider/operator capability


class ModelSelectionLearner(Recommender):
    kind = "model_selection"    # subject = reasoning-step signature, option = model tier


class PlanPolicyLearner(Recommender):
    kind = "plan_policy"        # subject = mission kind, option = plan signature


class LearningStack:
    """Holds the three learners and the promotion gate. Recommendations are shadow-only until a
    policy promotes them — the one place production routing can change."""

    def __init__(self, *, allow_auto_promote: bool = False):
        self.routing = RoutingLearner()
        self.models = ModelSelectionLearner()
        self.plans = PlanPolicyLearner()
        self.allow_auto_promote = allow_auto_promote
        self._promoted: dict[tuple[str, str], str] = {}

    def _learners(self):
        return (self.routing, self.models, self.plans)

    def recommendations(self) -> list[Recommendation]:
        out: list[Recommendation] = []
        for lr in self._learners():
            for r in lr.recommend():
                key = (r.kind, r.subject)
                if self._promoted.get(key) == r.recommended:
                    r.status = "promoted"
                elif self.allow_auto_promote:
                    r.status = "shadow"       # eligible; a promote() call still gates the mutation
                out.append(r)
        return out

    def promote(self, rec: Recommendation, *, actor: str = "policy") -> Recommendation:
        """Policy-constrained promotion — the ONLY path that changes production routing."""
        self._promoted[(rec.kind, rec.subject)] = rec.recommended
        rec.status = "promoted"
        return rec

    def promoted_choice(self, kind: str, subject: str) -> str | None:
        """The promoted option for a (kind, subject), if any — how the compiler/router reads a promotion."""
        return self._promoted.get((kind, subject))
