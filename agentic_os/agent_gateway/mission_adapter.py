"""Phase 2 — bind the gateway's Mission seams to the live Mission Runtime (plan §4).

``missions.delegate_goal`` is the flagship: an external agent hands a *goal*, and ReDevOps retains
control of execution — the Mission Runtime plans it, gates side effects on human approval, verifies,
and can pause/resume. The gateway does not re-implement any of that; this adapter is a thin, pure
mapping onto the runtime's public methods, so "policy cannot be bypassed" is a property of routing
through the runtime, not of gateway logic.

Duck-typed on purpose: it accepts any object exposing ``create_mission`` / ``run`` / ``missions`` /
``inbox`` / ``explain`` / ``suspend`` / ``resume`` (the real :class:`MissionRuntime`, or a test
double), so this module needs no import of the runtime and its heavy dependencies.
"""
from __future__ import annotations

from typing import Any, List

from .gateway import MissionDelegation
from .contracts import GatewayPrincipal


def _state_name(mission: Any) -> str:
    st = getattr(mission, "state", "")
    return getattr(st, "name", None) or str(st)


class MissionRuntimeAdapter:
    """Implements the gateway's ``MissionPort`` (delegate) and ``MissionReadPort`` (list/get/
    explain/pending_approvals) plus pause/resume control, over a Mission Runtime."""

    def __init__(self, runtime: Any, *, auto_run: bool = True) -> None:
        self.rt = runtime
        self.auto_run = auto_run          # start execution on delegate; the mission still gates itself

    # ── MissionPort ────────────────────────────────────────────────────────────
    def delegate(self, goal: str, *, constraints, principal: GatewayPrincipal,
                 arguments: dict) -> MissionDelegation:
        mission = self.rt.create_mission(goal, constraints=list(constraints or ()))
        state = _state_name(mission)
        if self.auto_run and state != "FAILED":
            # run() drives the governed graph and RETURNS parked at a human gate (WAITING_HUMAN),
            # succeeded, or blocked (FAILED) — it does not push past an approval on its own.
            mission = self.rt.run(mission.id)
            state = _state_name(mission)
        return MissionDelegation(mission_id=mission.id, state=state,
                                 needs_approval=(state == "WAITING_HUMAN"))

    # ── MissionReadPort ────────────────────────────────────────────────────────
    def list(self) -> List[dict]:
        return list(self.rt.missions())

    def get(self, mission_id: str) -> dict:
        for m in self.rt.missions():
            if m.get("id") == mission_id:
                return m
        return {"id": mission_id, "state": "unknown"}

    def explain(self, mission_id: str) -> dict:
        return self.rt.explain(mission_id)

    def pending_approvals(self) -> List[dict]:
        return list(self.rt.inbox())

    # ── control (bounded writes) ─────────────────────────────────────────────────
    def pause(self, mission_id: str, *, actor: str,
              reason: str = "paused via agent gateway") -> dict:
        m = self.rt.suspend(mission_id, actor=actor, reason=reason)
        return {"id": mission_id, "state": _state_name(m)}

    def resume(self, mission_id: str, *, actor: str) -> dict:
        m = self.rt.resume(mission_id, actor=actor)
        return {"id": mission_id, "state": _state_name(m)}
