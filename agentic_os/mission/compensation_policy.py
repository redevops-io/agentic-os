"""Mission failure policy — WHAT to do when a step/branch fails (docs/COMPENSATION.md).

  HALT_AND_REPORT     stop, compensate nothing, report succeeded effects (default for fork branches)
  COMPENSATE_DECLARED undo completed steps that DECLARE a compensating capability (default for sequential
                      missions: reverse order); irreversible steps are never "compensated", only listed
  CONTINUE_SIBLINGS   let running/ready sibling branches finish; compensate nothing automatically

Pure functions (no I/O, no runtime imports) so ``fork_join.py`` can call ``decide_on_branch_failure`` directly.
A succeeded sibling is NEVER compensated unless the policy is COMPENSATE_DECLARED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class FailurePolicy(str, Enum):
    HALT_AND_REPORT = "HALT_AND_REPORT"
    COMPENSATE_DECLARED = "COMPENSATE_DECLARED"
    CONTINUE_SIBLINGS = "CONTINUE_SIBLINGS"


class ActionKind(str, Enum):
    HALT = "HALT"                  # stop scheduling new work in this mission; compensate nothing
    COMPENSATE = "COMPENSATE"      # compensate ``compensate_nodes`` (reverse order), halt the rest
    CONTINUE = "CONTINUE"          # let sibling branches carry on; compensate nothing


@dataclass(frozen=True)
class FailureAction:
    kind: ActionKind
    policy: FailurePolicy
    reason: str
    compensate_nodes: tuple[str, ...] = ()        # node ids whose DECLARED undo should run (reverse order)
    not_reversible: tuple[dict, ...] = ()         # succeeded consequential nodes that cannot be undone
    not_compensated: tuple[str, ...] = ()         # reversible succeeded nodes left as-is by policy
    cancel_siblings: bool = True                  # stop running/pending siblings (False for CONTINUE_SIBLINGS)


def effective_policy(mission: Any, *, forked: bool = False) -> FailurePolicy:
    """The mission's declared policy, else the topology default. An unrecognised value fails SAFE to
    HALT_AND_REPORT (it can never widen into compensation)."""
    raw = (getattr(mission, "failure_policy", "") or "").strip()
    if not raw:
        return FailurePolicy.HALT_AND_REPORT if forked else FailurePolicy.COMPENSATE_DECLARED
    try:
        return FailurePolicy(raw.upper())
    except ValueError:
        return FailurePolicy.HALT_AND_REPORT


_OK = {"succeeded", "done", "success", "completed"}


def _nodes(branch: Any) -> list[Any]:
    if isinstance(branch, dict):
        return list(branch.get("nodes") or [])
    return list(getattr(branch, "nodes", None) or [])


def _get(o: Any, k: str, default=None):
    return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)


def _succeeded(branch: Any) -> bool:
    return str(_get(branch, "status", "") or "").lower() in _OK


def classify_nodes(nodes: Iterable[Any]) -> tuple[list[Any], list[dict], list[Any]]:
    """Split completed nodes into (declared-undo, not_reversible entries, non-consequential). A node is
    reversible ONLY if it carries a declared ``undo``; side-effecting without one is irreversible/unknown."""
    rev, irrev, inert = [], [], []
    for n in nodes:
        if not _get(n, "side_effecting", False):
            inert.append(n)
        elif _get(n, "undo"):
            rev.append(n)
        else:
            irrev.append({"node_id": _get(n, "id"), "capability": _get(n, "capability"),
                          "reversibility": "IRREVERSIBLE" if _get(n, "irreversible", False) else "UNKNOWN"})
    return rev, irrev, inert


def decide_on_branch_failure(mission: Any, failed_branch: Any, siblings: Iterable[Any]) -> FailureAction:
    """What to do when ``failed_branch`` fails while ``siblings`` are in any state.

    Branches/siblings are duck-typed (attrs or dict keys): ``id``, ``status`` ("succeeded"/"done"/... counts as
    succeeded) and ``nodes`` (each with ``id``, ``capability``, ``side_effecting``, ``undo``, ``irreversible``).
    The failed branch's OWN completed nodes are considered too (a partially executed branch)."""
    policy = effective_policy(mission, forked=True)
    sibs = list(siblings or [])
    done_nodes: list[Any] = [n for b in [failed_branch, *sibs] if b is failed_branch or _succeeded(b)
                             for n in _nodes(b) if str(_get(n, "status", "done") or "done").lower() in _OK]
    rev, irrev, _ = classify_nodes(done_nodes)
    bid = _get(failed_branch, "id", "?")
    if policy is FailurePolicy.COMPENSATE_DECLARED:
        ids = tuple(_get(n, "id") for n in reversed(rev))
        return FailureAction(ActionKind.COMPENSATE, policy, f"branch {bid} failed; policy compensates declared undos",
                             compensate_nodes=ids, not_reversible=tuple(irrev))
    kind = ActionKind.CONTINUE if policy is FailurePolicy.CONTINUE_SIBLINGS else ActionKind.HALT
    return FailureAction(kind, policy, f"branch {bid} failed; policy {policy.value}: succeeded effects are kept",
                         not_reversible=tuple(irrev), not_compensated=tuple(_get(n, "id") for n in reversed(rev)),
                         cancel_siblings=kind is ActionKind.HALT)
