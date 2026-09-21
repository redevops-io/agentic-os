"""Phase 2 — inbound Mission delegation for external agents (plan §5, §11 Phase 2).

An external agent submits a GOAL (e.g. "inspect my infra; fix low-risk automatically, ask before
blocking traffic"). This bridge maps it onto the REAL Mission Runtime — it creates/continues a mission
in the runtime and never keeps a parallel, agent-owned mission state. The natural-language permissions
travel as *requested constraints* on the mission; Governance (the runtime's own gates) validates them.

What this deliberately does NOT do: it does not authorize. Feeding a mission input via
:meth:`provide_context` answers a disambiguation gate (bounded input) — it never satisfies an approval
gate. Approvals happen only through the trusted surface (:mod:`.approval_bridge`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .contracts import AgentTaskInput, AgentTaskRequest


class InboundError(RuntimeError):
    pass


@dataclass
class MissionHandle:
    mission_id: str
    state: str
    needs_approval: bool = False

    def as_dict(self) -> dict:
        return {"mission_id": self.mission_id, "state": self.state, "needs_approval": self.needs_approval}


def _state_name(mission: Any) -> str:
    st = getattr(mission, "state", None)
    return getattr(st, "name", None) or getattr(st, "value", None) or str(st)


@dataclass
class InboundExternalAgentBridge:
    """Wraps the real MissionRuntime (duck-typed on its public contract:
    create_mission / run / approve / missions / inbox / explain). Stateless beyond the runtime."""

    runtime: Any

    def submit_goal(self, request: AgentTaskRequest) -> MissionHandle:
        if not request.goal.strip():
            raise InboundError("malformed inbound request: empty goal")            # fail closed
        constraints = self._requested_constraints(request)
        mission = self.runtime.create_mission(request.goal, constraints=constraints)
        ran = self.runtime.run(mission.id)
        state = _state_name(ran)
        return MissionHandle(mission_id=ran.id, state=state, needs_approval=(state == "WAITING_HUMAN"))

    def provide_context(self, mission_id: str, node_id: str, value: AgentTaskInput) -> MissionHandle:
        """Answer a disambiguation gate with bounded input. NOT an approval — the runtime rejects using
        this to satisfy an approval/verification gate; it only supplies a requested value."""
        mission = self.runtime.approve(mission_id, node_id, "edit", {"value": dict(value.value)})
        state = _state_name(mission)
        return MissionHandle(mission_id=mission_id, state=state, needs_approval=(state == "WAITING_HUMAN"))

    def status(self, mission_id: str) -> dict:
        for m in self.runtime.missions():
            if m.get("id") == mission_id:
                return m
        raise InboundError(f"no such mission: {mission_id}")

    def findings(self, mission_id: str) -> dict:
        return self.runtime.explain(mission_id)

    def pending_human_tasks(self) -> List[dict]:
        return list(self.runtime.inbox())

    def _requested_constraints(self, request: AgentTaskRequest) -> List[str]:
        scope = request.permission_scope
        constraints: List[str] = []
        for cap in scope.ask_before_capabilities:
            constraints.append(f"ask-before:{cap}")            # requested, validated by the runtime's gates
        for cap in scope.auto_approve_capabilities:
            constraints.append(f"auto-approve-if-policy:{cap}")
        if request.project_id:
            constraints.append(f"project:{request.project_id}")
        return constraints
