"""MissionRuntimeLauncher — the concrete MissionLauncher onto a Mission Runtime (plan §3.4).

The Decision->Mission bridge emits a ``MissionRequest`` and calls a ``MissionLauncher``; this is the
launcher that authors a real governed mission on a ``MissionRuntime``. It uses the runtime's own
designed path — state the need, the runtime discovers and binds the capability and gates it per the
bound capability's ``approval_required`` — rather than fighting the planner by forcing a binding.
Reused by every app migration, so apps never touch the runtime's authoring internals directly.

``drive`` is a convenience for surfaces/tests that want to run a mission to completion synchronously,
auto-approving gates through a supplied approver.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .decision_bridge import MissionRequest


@dataclass
class MissionRuntimeLauncher:
    """A ``MissionLauncher`` backed by a ``MissionRuntime`` (duck-typed on ``create_mission``)."""

    runtime: Any
    policy_refs: Sequence[str] = field(default_factory=lambda: ("*",))

    def launch(self, request: MissionRequest) -> str:
        # Carry the decision's INPUTS onto the mission (they were being dropped), so the bound node runs with
        # the evidence/parameters the decision was made on.
        mission = self.runtime.create_mission(
            request.goal, policy_refs=list(self.policy_refs), inputs=dict(request.inputs or {}))
        mid = getattr(mission, "id", "") or getattr(mission, "mission_id", "")
        # Safety (plan §3.4): the compiled plan must bind the capability the DECISION selected. An ambiguous
        # goal can otherwise let discovery bind a DIFFERENT capability — a gated `send_email` decision
        # resolving to an ungated `delete_all` — which would sidestep the gate. If the selected capability is
        # not in the plan, REFUSE the mission rather than run it. Best-effort: enforced only when the runtime
        # exposes its compiled plan (``_plans``); a launcher over a runtime that doesn't is left unchanged.
        if request.capability and mid:
            plans = getattr(self.runtime, "_plans", None)
            plan = plans.get(mid) if isinstance(plans, dict) else None
            graph = getattr(plan, "graph", None)
            bound = {getattr(n, "capability", "") for n in getattr(graph, "nodes", ())} if graph else set()
            if bound and request.capability not in bound:
                from .decision_bridge import BridgeError
                raise BridgeError(
                    f"planner bound {sorted(bound)} but the decision selected {request.capability!r}; "
                    "refusing to run a mission that does not bind the selected capability")
        return mid


# An approver decides a parked gate: (mission_id, node_id) -> "approve" | "reject".
Approver = Callable[[str, str], str]


def _auto_approve(_mission_id: str, _node_id: str) -> str:
    return "approve"


def drive(runtime: Any, mission_id: str, *, approver: Optional[Approver] = None,
          max_gates: int = 16) -> Any:
    """Run a mission to a terminal state, resolving any human gate via ``approver`` (default: approve).

    Returns the mission object. Duck-typed on ``run``/``approve`` and on reading the pending gate from
    ``runtime.repo.pending_human(mission_id)``; tolerant of runtimes that expose a mission's state
    differently.
    """
    approve = approver or _auto_approve
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


__all__ = ["MissionRuntimeLauncher", "Approver", "drive"]
