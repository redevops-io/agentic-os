"""Runtime-boundary security telemetry (opt-in).

The Executor calls :meth:`SecurityMonitor.observe` at the capability boundary — outside the operator /
model's control — so consequential operations produce canonical ``RuntimeSecurityEvent``s whether or not
the agent reports them. Each event is folded into an append-only ``SecurityTrajectory``; :meth:`disposition`
correlates the *series* (individually-permissible events can compose into an unacceptable trajectory) and,
when the planned capability set is known, flags plan-vs-observed divergence.

The trustworthy facts come from the runtime, not the agent: which capability ran (the node), what it is
*allowed* to do (its `CapabilityDescriptor` — declared network / required authority / isolation), and
whether it succeeded. Output volume is observed from the result at the boundary. Nothing here depends on
the operator choosing to disclose what it did.

Requires runtime-contracts (the canonical telemetry protocol); imported lazily so the runtime has no hard
dependency until telemetry is switched on.
"""
from __future__ import annotations

from typing import Any, Callable

from .types import Node


class SecurityMonitor:
    def __init__(self, *, mission_id: str = "", planned_capabilities: tuple[str, ...] = (),
                 descriptor_for: "Callable[[str], Any] | None" = None):
        from runtime_contracts import Containment, SecurityTrajectory   # noqa: PLC0415 — lazy: opt-in
        self.mission_id = mission_id
        self.planned = tuple(planned_capabilities)
        self.descriptor_for = descriptor_for   # capability id -> CapabilityDescriptor (declared surface)
        self.trajectory = SecurityTrajectory()
        self.containment = Containment()
        self._seq = 0

    def observe(self, node: Node, result: dict | None, *, isolation: str, error: str | None = None) -> None:
        from runtime_contracts import RuntimeSecurityEvent, SecurityEventType, TelemetryKind  # noqa: PLC0415
        self._seq += 1
        desc = self.descriptor_for(node.capability) if self.descriptor_for else None
        # Declared surface — from the registry, not the agent: what this capability is ALLOWED to reach.
        network = tuple(getattr(desc, "network", ()) or ()) if desc else ()
        permissions = tuple(getattr(desc, "required_authority", ()) or ()) if desc else ()
        classifications = tuple(getattr(desc, "data_classifications", ()) or ()) if desc else ()
        # Boundary-observed output facts (result size / declared side effects), not agent prose.
        side_effects: tuple[str, ...] = ()
        if isinstance(result, dict):
            se = result.get("side_effects")
            if isinstance(se, (list, tuple)):
                side_effects = tuple(str(s) for s in se)
            n = result.get("records") or result.get("records_read")
            if isinstance(n, int):
                side_effects = side_effects + (f"records_read={n}",)

        etype = SecurityEventType.CAPABILITY_INVOKED.value
        if error is not None:
            etype = SecurityEventType.SANDBOX_VIOLATION.value if isolation in {"sandbox", "strict"} \
                else SecurityEventType.CAPABILITY_INVOKED.value
        elif network:
            etype = SecurityEventType.NETWORK_ACCESS.value

        self.trajectory.add(RuntimeSecurityEvent(
            event_id=f"{self.mission_id or 'm'}:{node.capability}:{self._seq}",
            kind=TelemetryKind.SECURITY if error is not None else TelemetryKind.EXECUTION,
            event_type=etype, sequence=self._seq, mission_id=self.mission_id, capability=node.capability,
            permissions_exercised=permissions, network=network, data_classifications=classifications,
            side_effects=side_effects, result=("error" if error is not None else "ok")))

    def disposition(self, **thresholds):
        """Correlate the trajectory-so-far into (GovernanceDisposition, reasons)."""
        from runtime_contracts import correlate   # noqa: PLC0415
        return correlate(self.trajectory, planned_capabilities=self.planned, **thresholds)

    def enforce(self, **thresholds):
        """Correlate AND drive the containment state machine off the disposition. Returns
        (GovernanceDisposition, reasons, ContainmentState) — the full runtime security loop:
        action → telemetry → trajectory → correlation → disposition → containment."""
        disp, reasons = self.disposition(**thresholds)
        state = self.containment.on_disposition(disp)
        return disp, reasons, state
