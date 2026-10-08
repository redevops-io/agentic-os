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
        mission = self.runtime.create_mission(request.goal, policy_refs=list(self.policy_refs))
        return getattr(mission, "id", "") or getattr(mission, "mission_id", "")


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
