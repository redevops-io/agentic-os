"""Per-scope learning for the growth agent (Growth Intelligence — the outcome loop).

The agent proposes, a human approves or dismisses, and the action either works or doesn't. This records those
outcomes per **scope** (a scope is whatever a deployment isolates on — typically one site) and turns them into
a per-scope, per-action-kind **priority weight**: action kinds that a scope's operators keep approving and that
keep working float up; kinds they keep dismissing or that don't pan out sink. Learning is scoped, so one site's
history never re-weights another's.

It only re-orders the queue — it never changes the governance gate, and it never invents an outcome. Storage is
an interface (`OutcomeLedger`); the in-memory ledger suffices for tests and a first deployment, a store-backed
one persists across ticks. Weights default to 1.0 (no effect) until there's evidence, and are smoothed so a
single outcome can't swing them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Protocol


class Decision(str, Enum):
    APPROVED = "APPROVED"
    DISMISSED = "DISMISSED"


class Result(str, Enum):
    SUCCEEDED = "SUCCEEDED"      # the approved action achieved its goal
    FAILED = "FAILED"           # it was tried and didn't
    UNKNOWN = "UNKNOWN"         # not yet known / not measured


@dataclass(frozen=True)
class Outcome:
    scope: str                   # the isolation scope (e.g. a site id)
    action_kind: str             # the signal/action kind the decision was about
    decision: Decision
    result: Result = Result.UNKNOWN
    candidate_id: str = ""
    at_ms: int = 0


class OutcomeLedger(Protocol):
    def record(self, outcome: Outcome) -> None: ...
    def outcomes_for(self, scope: str) -> List[Outcome]: ...


@dataclass
class InMemoryOutcomeLedger:
    _rows: List[Outcome] = field(default_factory=list)

    def record(self, outcome: Outcome) -> None:
        self._rows.append(outcome)

    def outcomes_for(self, scope: str) -> List[Outcome]:
        return [o for o in self._rows if o.scope == scope]


_PRIOR = 1.0   # Laplace smoothing: 1 pseudo-count each side → no data ⇒ 0.5 signal ⇒ weight 1.0


def _rate(pos: int, neg: int) -> float:
    return (pos + _PRIOR) / (pos + neg + 2 * _PRIOR)


def scope_weights(ledger: OutcomeLedger, scope: str) -> Dict[str, float]:
    """Learned priority multiplier per action kind for a scope, in ~[0.5, 1.5]. Combines how often operators
    approve a kind with how often approved actions of that kind succeed; smoothed so one outcome barely moves
    it, and 1.0 (no effect) when there's no evidence."""
    approved: Dict[str, int] = {}
    dismissed: Dict[str, int] = {}
    succeeded: Dict[str, int] = {}
    failed: Dict[str, int] = {}
    kinds = set()
    for o in ledger.outcomes_for(scope):
        kinds.add(o.action_kind)
        if o.decision is Decision.APPROVED:
            approved[o.action_kind] = approved.get(o.action_kind, 0) + 1
        elif o.decision is Decision.DISMISSED:
            dismissed[o.action_kind] = dismissed.get(o.action_kind, 0) + 1
        if o.result is Result.SUCCEEDED:
            succeeded[o.action_kind] = succeeded.get(o.action_kind, 0) + 1
        elif o.result is Result.FAILED:
            failed[o.action_kind] = failed.get(o.action_kind, 0) + 1
    weights: Dict[str, float] = {}
    for k in kinds:
        approve_signal = _rate(approved.get(k, 0), dismissed.get(k, 0))     # 0..1, .5 = neutral
        success_signal = _rate(succeeded.get(k, 0), failed.get(k, 0))       # 0..1, .5 = neutral
        weights[k] = round(0.5 + 0.5 * approve_signal + 0.5 * success_signal, 4)   # .5=neutral→1.0
    return weights


def apply_weights(rows: List[Dict[str, Any]], weights: Dict[str, float]) -> List[Dict[str, Any]]:
    """Re-order goal-ranked rows by learned priority (priority × the scope weight for the row's action kind),
    keeping goal-aligned rows first. Adds `learned_priority` + `learned_weight`; never drops or gates."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        w = weights.get(r.get("action_kind", ""), 1.0)
        out.append({**r, "learned_weight": w, "learned_priority": round(r.get("priority", 0.0) * w, 4)})
    out.sort(key=lambda r: (r.get("goal_aligned", False), r["learned_priority"]), reverse=True)
    return out
