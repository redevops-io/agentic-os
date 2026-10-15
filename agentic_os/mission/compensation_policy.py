"""Mission failure policy — WHAT to do when a step/branch fails (docs/COMPENSATION.md).

  HALT_AND_REPORT     stop, compensate nothing, report succeeded effects (default for fork branches)
  PRESERVE_ONLY       HALT_AND_REPORT plus: only TEMPORARY resources a capability declares ``disposition=CLEANUP``
                      (with preconditions + authorization) are cleaned up; every successful effect is preserved.
                      Default of the kernel templates onboarding / deploy_app / sky_deploy.
  COMPENSATE_DECLARED undo completed steps that DECLARE a compensating capability (default for sequential
                      missions: reverse order); irreversible steps are never "compensated", only listed
  CONTINUE_SIBLINGS   let running/ready sibling branches finish; compensate nothing automatically

Pure functions (no I/O, no runtime imports) so ``fork_join.py`` can call ``decide_on_branch_failure`` directly.
A succeeded sibling is NEVER compensated unless the policy is COMPENSATE_DECLARED.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class FailurePolicy(str, Enum):
    HALT_AND_REPORT = "HALT_AND_REPORT"
    PRESERVE_ONLY = "PRESERVE_ONLY"
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


ENV_DEFAULT_POLICY = "AGENTIC_OS_DEFAULT_FAILURE_POLICY"


def is_forked(plan_or_graph: Any) -> bool:
    """Single source of truth for 'this plan has parallel fork branches' (ExecutionGraph.forks, set by the
    fork/join executor). Used by run_saga; decide_on_branch_failure is forked by definition."""
    g = getattr(plan_or_graph, "graph", plan_or_graph)
    return bool(getattr(g, "forks", None))


def _parse(raw: str) -> "FailurePolicy | None":
    try:
        return FailurePolicy((raw or "").strip().upper()) if (raw or "").strip() else None
    except ValueError:
        return FailurePolicy.HALT_AND_REPORT       # unrecognised: fail SAFE, never widen into compensation


_LOG = logging.getLogger("agentic_os.mission.compensation_policy")
_WARNED: set = set()
ENV_RERESOLVE_LEGACY = "AGENTIC_OS_RERESOLVE_LEGACY_POLICY"
SRC_ENV, SRC_ENV_INVALID, SRC_TEMPLATE, SRC_DEFAULT = "env", "env_invalid", "template", "default"


def _warn_once(key: tuple, msg: str, level=logging.WARNING) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        _LOG.log(level, msg)


def resolve_default_detailed(template: "str | None") -> "tuple[FailurePolicy, str, str]":
    """-> (policy, source, warning). ``source`` is ``env`` | ``env_invalid`` | ``template`` | ``default``; ``warning`` is
    non-empty when the deployment env silently changed what a template declares, or the env value is unrecognised
    (fail SAFE to HALT_AND_REPORT, which also drops PRESERVE_ONLY's CLEANUP step - so it is reported loudly)."""
    from .templates import TEMPLATE_FAILURE_POLICY  # noqa: PLC0415
    tpl = _parse(TEMPLATE_FAILURE_POLICY.get(template or "", ""))
    raw = os.environ.get(ENV_DEFAULT_POLICY, "").strip()
    if raw:
        env = _parse(raw)
        valid = raw.upper() in FailurePolicy.__members__
        if not valid:
            w = (f"{ENV_DEFAULT_POLICY}={raw!r} is not a failure policy {sorted(FailurePolicy.__members__)}: "
                 "failing safe to HALT_AND_REPORT (nothing is compensated, not even PRESERVE_ONLY cleanup)")
            _warn_once(("invalid", raw), w, logging.ERROR)
            return env, SRC_ENV_INVALID, w
        _warn_once(("env", raw), f"{ENV_DEFAULT_POLICY}={env.value} is set: it applies to every mission that does "
                                 "not choose its own failure_policy")
        if tpl is not None and tpl is not env:
            w = (f"{ENV_DEFAULT_POLICY}={env.value} overrides the template default {tpl.value} of '{template}'")
            _warn_once(("override", raw, template), w)
            return env, SRC_ENV, w
        return env, SRC_ENV, ""
    if tpl is not None:
        return tpl, SRC_TEMPLATE, ""
    return FailurePolicy.HALT_AND_REPORT, SRC_DEFAULT, ""


def resolve_default(template: "str | None") -> "FailurePolicy":
    """The non-forked default for a template: the deployment env (an explicit deployment decision, e.g.
    ``AGENTIC_OS_DEFAULT_FAILURE_POLICY=COMPENSATE_DECLARED`` to opt every workflow back in to compensation), then the
    template-declared default (kernel templates: PRESERVE_ONLY), then HALT_AND_REPORT. Resolved ONCE at mission creation
    and frozen into ``MissionCreated`` as ``default_failure_policy`` (+ ``default_policy_source``/``_warning``) so a
    later env/template change cannot alter how an old mission replays."""
    return resolve_default_detailed(template)[0]


def is_legacy_frozen(mission: Any) -> bool:
    """A mission created before ``default_policy_source`` existed (<= 0.3.32) whose frozen default is
    COMPENSATE_DECLARED for a template whose default is NOW PRESERVE_ONLY: it keeps auto-reversing (the frozen value is
    authoritative for replay) unless the operator sets ``AGENTIC_OS_RERESOLVE_LEGACY_POLICY=1``."""
    from .templates import TEMPLATE_FAILURE_POLICY  # noqa: PLC0415
    return (not getattr(mission, "failure_policy", "") and not getattr(mission, "default_policy_source", "")
            and _parse(getattr(mission, "default_failure_policy", "")) is FailurePolicy.COMPENSATE_DECLARED
            and _parse(TEMPLATE_FAILURE_POLICY.get(getattr(mission, "template", None) or "", "")) is
            FailurePolicy.PRESERVE_ONLY)


def effective_policy(mission: Any, *, forked: bool = False) -> FailurePolicy:
    """Resolution order: the mission's own ``failure_policy`` (explicit opt-in), then the default FROZEN at
    creation (``mission.default_failure_policy``: ``AGENTIC_OS_DEFAULT_FAILURE_POLICY``, else the template default,
    else HALT_AND_REPORT), and only for a legacy mission without a frozen default the live template/env
    resolution. Nothing compensates unless one of them says COMPENSATE_DECLARED. Fork branches ignore the
    default and use HALT_AND_REPORT unless the mission itself opts in."""
    own = _parse(getattr(mission, "failure_policy", ""))
    if own is not None:
        return own
    if forked:
        return FailurePolicy.HALT_AND_REPORT
    frozen = _parse(getattr(mission, "default_failure_policy", ""))
    if frozen is not None:
        if is_legacy_frozen(mission) and os.environ.get(ENV_RERESOLVE_LEGACY, "").strip().lower() in ("1", "true", "yes"):
            return resolve_default(getattr(mission, "template", None))
        return frozen
    return resolve_default(getattr(mission, "template", None))


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
        elif _get(n, "disposition") == "PRESERVE":
            irrev.append({"node_id": _get(n, "id"), "capability": _get(n, "capability"),
                          "reversibility": "IRREVERSIBLE", "disposition": "PRESERVE"})
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
    if policy is FailurePolicy.PRESERVE_ONLY:
        cleanup = [n for n in rev if _get(n, "disposition") == "CLEANUP"]
        kept = [n for n in rev if n not in cleanup]
        kept_ids = tuple(_get(n, "id") for n in reversed(kept))
        if cleanup:
            return FailureAction(ActionKind.COMPENSATE, policy,
                                 f"branch {bid} failed; policy PRESERVE_ONLY cleans up declared temporary resources only",
                                 compensate_nodes=tuple(_get(n, "id") for n in reversed(cleanup)),
                                 not_reversible=tuple(irrev), not_compensated=kept_ids)
        return FailureAction(ActionKind.HALT, policy,
                             f"branch {bid} failed; policy PRESERVE_ONLY: succeeded effects are kept",
                             not_reversible=tuple(irrev), not_compensated=kept_ids, cancel_siblings=True)
    if policy is FailurePolicy.COMPENSATE_DECLARED:
        ids = tuple(_get(n, "id") for n in reversed(rev))
        return FailureAction(ActionKind.COMPENSATE, policy, f"branch {bid} failed; policy compensates declared undos",
                             compensate_nodes=ids, not_reversible=tuple(irrev))
    kind = ActionKind.CONTINUE if policy is FailurePolicy.CONTINUE_SIBLINGS else ActionKind.HALT
    return FailureAction(kind, policy, f"branch {bid} failed; policy {policy.value}: succeeded effects are kept",
                         not_reversible=tuple(irrev), not_compensated=tuple(_get(n, "id") for n in reversed(rev)),
                         cancel_siblings=kind is ActionKind.HALT)
