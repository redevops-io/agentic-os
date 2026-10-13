"""MissionRuntimeLauncher — the concrete MissionLauncher onto a Mission Runtime (plan §3.4).

The Decision->Mission bridge emits a ``MissionRequest`` and calls a ``MissionLauncher``; this is the
launcher that authors a real governed mission on a ``MissionRuntime``. It uses the runtime's own
designed path — state the need, the runtime discovers and binds the capability and gates it per the
bound capability's ``approval_required`` — rather than fighting the planner by forcing a binding.
Reused by every app migration, so apps never touch the runtime's authoring internals directly.

``drive`` is a convenience for surfaces/tests that want to run a mission to completion synchronously,
resolving gates through a supplied (required) approver.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .decision_bridge import LaunchRefused, MissionRequest


@dataclass
class MissionRuntimeLauncher:
    """A ``MissionLauncher`` backed by a ``MissionRuntime`` (duck-typed on ``create_mission``)."""

    runtime: Any
    policy_refs: Sequence[str] = field(default_factory=lambda: ("*",))
    principal: Any = None        # default delegating principal when the request carries none

    def launch(self, request: MissionRequest) -> str:
        """Author a mission for the decision — but only if its compiled plan is EXACTLY what was selected.

        Planning binds by goal text, so an ambiguous goal can resolve to a different capability (a gated
        ``send_email`` decision becoming an ungated ``delete_all``). The plan is therefore inspected right
        after compilation and BEFORE anything runs; the launch is refused (:class:`LaunchRefused`, the
        mission is created-and-cancelled with a ``MissionCancelled`` event, nothing executes) unless the
        plan binds the selected capability AND binds no side-effecting capability the decision did not
        select. Read-only extras are allowed. Fail-closed: a runtime whose plan cannot be inspected is
        refused. The delegating principal and ``intervention_id`` are carried onto the mission."""
        import inspect

        kwargs: dict = {"policy_refs": list(self.policy_refs), "inputs": dict(request.inputs or {})}
        principal = getattr(request, "principal", None) or self.principal
        try:
            accepted = set(inspect.signature(self.runtime.create_mission).parameters)
        except (TypeError, ValueError):
            accepted = set()
        has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in
                         inspect.signature(self.runtime.create_mission).parameters.values()) if accepted else False
        for key, val in (("principal", principal), ("intervention_id", request.intervention_id)):
            if val and (key in accepted or has_var_kw):
                kwargs[key] = val
        mission = self.runtime.create_mission(request.goal, **kwargs)
        mid = getattr(mission, "id", "") or getattr(mission, "mission_id", "")
        if request.capability:
            problem = self._plan_problem(mid, request)
            if problem:
                self._cancel(mid, problem)
                raise LaunchRefused(problem)
        return mid

    def _plan_problem(self, mid: str, request: MissionRequest) -> str:
        plans = getattr(self.runtime, "_plans", None)
        plan = plans.get(mid) if isinstance(plans, dict) else None
        nodes = list(getattr(getattr(plan, "graph", None), "nodes", ()) or ())
        if not nodes:
            return (f"no compiled plan to inspect for mission {mid!r}; refusing to run a mission whose "
                    f"binding to {request.capability!r} cannot be verified")
        bound = {getattr(n, "capability", "") for n in nodes}
        if request.capability not in bound:
            return (f"planner bound {sorted(bound)} but the decision selected {request.capability!r}; "
                    "refusing to run a mission that does not bind the selected capability")
        selected = {request.capability, *request.required_capabilities}
        extra = sorted({getattr(n, "capability", "") for n in nodes
                        if getattr(n, "capability", "") not in selected and getattr(n, "side_effecting", False)})
        if extra:
            return (f"plan binds side-effecting {extra} that the decision did not select "
                    f"({request.capability!r}); refusing")
        return ""

    def _cancel(self, mid: str, reason: str) -> None:
        cancel = getattr(self.runtime, "cancel_mission", None)
        if mid and callable(cancel):
            try:
                cancel(mid, reason)
            except Exception:  # noqa: BLE001 — refusal must still raise
                pass


# An approver decides a parked gate: (mission_id, node_id) -> "approve" | "reject".
Approver = Callable[[str, str], str]


def auto_approve_for_tests(_mission_id: str, _node_id: str) -> str:
    """Approve every gate. TEST/DEMO ONLY — never wire this into a production surface."""
    return "approve"


def drive(runtime: Any, mission_id: str, *, approver: Approver, max_gates: int = 16) -> Any:
    """Run a mission to a terminal state, resolving any human gate via the REQUIRED ``approver``.

    There is deliberately no default: silently approving gates made ``drive`` a gate bypass. Tests pass
    ``auto_approve_for_tests``; a real surface passes a callable that asks a human.
    """
    approve = approver
    runtime.run(mission_id)
    for _ in range(max_gates):
        pending = None
        repo = getattr(runtime, "repo", None)
        if repo is not None and hasattr(repo, "pending_human"):
            pending = repo.pending_human(mission_id)
        if not pending:
            break
        node_id = pending["node_id"] if isinstance(pending, dict) else pending
        decision = approve(mission_id, node_id)
        runtime.approve(mission_id, node_id, decision)
        if decision != "approve":
            break
    missions = getattr(runtime, "_missions", {})
    return missions.get(mission_id)


__all__ = ["MissionRuntimeLauncher", "Approver", "drive", "auto_approve_for_tests", "LaunchRefused"]
