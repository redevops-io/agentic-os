"""Mission lifecycle contributors — host-agnostic hooks the runtime dispatches at key phases.

The contract (from xai-grok-build's agent lifecycle): a contributor **receives data-only inputs at
dispatch time, acts only through capabilities injected at install time, and never owns loop
control**. So a contributor cannot alter the plan, block a node, or steer the scheduler — it observes
(and may act through an injected capability, e.g. append evidence, emit a metric, wake the monitor),
and the Mission Runtime keeps the loop. This is the same invariant as the Context Runtime resolve
door: orchestration decides; extensions state needs and act through granted capabilities.

The registry is optional and no-op when empty — existing runtimes are unaffected. A contributor that
raises is swallowed (an extension must never break orchestration).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


# ── data-only lifecycle events (no methods, no runtime handles) ──
@dataclass(frozen=True)
class MissionStarted:
    mission_id: str
    goal: str
    template: str | None = None


@dataclass(frozen=True)
class NodeCompleted:
    mission_id: str
    node_id: str
    capability: str
    status: str


@dataclass(frozen=True)
class GateReached:
    mission_id: str
    node_id: str
    capability: str


@dataclass(frozen=True)
class MissionFinished:
    mission_id: str
    state: str          # succeeded | failed


@dataclass(frozen=True)
class SessionIdle:
    reason: str = "idle"


# event type → the contributor method that handles it
_HANDLER = {
    MissionStarted: "on_mission_started",
    NodeCompleted: "on_node_completed",
    GateReached: "on_gate_reached",
    MissionFinished: "on_mission_finished",
    SessionIdle: "on_session_idle",
}


class LifecycleContributor:
    """Base class — override the hooks you care about. Each receives ``(event, capabilities)`` where
    ``capabilities`` is the dict of injected data-services the contributor may act through. Return
    value is ignored (no loop control)."""

    def on_mission_started(self, event: MissionStarted, capabilities: dict[str, Any]) -> None: ...
    def on_node_completed(self, event: NodeCompleted, capabilities: dict[str, Any]) -> None: ...
    def on_gate_reached(self, event: GateReached, capabilities: dict[str, Any]) -> None: ...
    def on_mission_finished(self, event: MissionFinished, capabilities: dict[str, Any]) -> None: ...
    def on_session_idle(self, event: SessionIdle, capabilities: dict[str, Any]) -> None: ...


class LifecycleRegistry:
    """Holds contributors + the injected capabilities, and dispatches events to them. No-op when
    empty. Capabilities are injected once at install time; events carry only data at dispatch."""

    def __init__(self, capabilities: dict[str, Any] | None = None):
        self._contributors: list[Any] = []
        self.capabilities: dict[str, Any] = capabilities or {}

    def install(self, contributor: Any) -> "LifecycleRegistry":
        self._contributors.append(contributor)
        return self

    def inject(self, name: str, capability: Any) -> "LifecycleRegistry":
        self.capabilities[name] = capability
        return self

    def dispatch(self, event: Any) -> None:
        method = _HANDLER.get(type(event))
        if method is None:
            return
        for c in self._contributors:
            fn: Callable | None = getattr(c, method, None)
            if fn is None:
                continue
            try:
                fn(event, self.capabilities)          # data-only in; return ignored (no loop control)
            except Exception:  # noqa: BLE001 — an extension must never break the mission loop
                pass
