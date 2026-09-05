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
                 descriptor_for: "Callable[[str], Any] | None" = None,
                 sink: "Callable[[Any], None] | None" = None):
        from runtime_contracts import Containment, SecurityTrajectory   # noqa: PLC0415 — lazy: opt-in
        self.mission_id = mission_id
        self.planned = tuple(planned_capabilities)
        self.descriptor_for = descriptor_for   # capability id -> CapabilityDescriptor (declared surface)
        self.trajectory = SecurityTrajectory()
        self.containment = Containment()
        self._seq = 0
        # Optional durable sink: each boundary event is ALSO handed here (e.g. appended to the event
        # store — see durable_sink). None ⇒ in-memory trajectory only, unchanged. A sink never breaks
        # execution (mirrors the event-log subscriber discipline).
        self._sink = sink

    def _emit(self, ev: "Any") -> None:
        if self._sink is None:
            return
        try:
            self._sink(ev)
        except Exception:   # a telemetry sink must never break the boundary it observes
            pass

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

        ev = RuntimeSecurityEvent(
            event_id=f"{self.mission_id or 'm'}:{node.capability}:{self._seq}",
            kind=TelemetryKind.SECURITY if error is not None else TelemetryKind.EXECUTION,
            event_type=etype, sequence=self._seq, mission_id=self.mission_id, capability=node.capability,
            permissions_exercised=permissions, network=network, data_classifications=classifications,
            side_effects=side_effects, result=("error" if error is not None else "ok"))
        self.trajectory.add(ev)
        self._emit(ev)

    def observe_credential(self, node: Node, event_type: str, *, grant: "Any | None" = None,
                           reason: str = "") -> None:
        """Record a canonical credential event (issued/redeemed/revoked/denied) at the boundary — SAFE
        fields only: grant id + secret-ref fingerprint + authority ref, never the secret. This is the
        authoritative fact that authority to use a credential was granted/exercised; the agent cannot
        suppress it. Governance can then correlate sensitive-read authority with egress."""
        from runtime_contracts import RuntimeSecurityEvent, TelemetryKind  # noqa: PLC0415
        self._seq += 1
        refs: tuple[str, ...] = ()
        authority_ref = ""
        if grant is not None:
            authority_ref = getattr(grant, "authority_ref", "") or ""
            cref = getattr(grant, "credential_ref", None)
            fp = cref.fingerprint() if cref is not None and hasattr(cref, "fingerprint") else ""
            refs = tuple(x for x in (f"grant:{getattr(grant, 'grant_id', '')}", fp and f"secret:{fp}") if x)
        ev = RuntimeSecurityEvent(
            event_id=f"{self.mission_id or 'm'}:{node.capability}:cred:{self._seq}",
            kind=TelemetryKind.SECURITY, event_type=event_type, sequence=self._seq,
            mission_id=self.mission_id, capability=node.capability, authority_chain_ref=authority_ref,
            evidence_refs=refs, decision=event_type, result=("error" if "DENIED" in event_type else "ok"))
        self.trajectory.add(ev)
        self._emit(ev)

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


TELEMETRY_SCOPE = "__telemetry__"


def durable_sink(store, *, scope: str = TELEMETRY_SCOPE) -> "Callable[[Any], None]":
    """A :class:`SecurityMonitor` sink that appends each boundary event to the event store.

    Because the runtime is event-sourced, this makes runtime-security telemetry **durable and
    queryable** with no separate logging system: pair it with a DuckDB/Postgres backend and the
    security trajectory is persisted alongside mission events, per mission, for governance to read.
    The events are already the redacted boundary facts (capability, declared surface, grant ids and
    secret-ref *fingerprints* — never secret values), so this is safe by construction. Events are
    stored under their own ``mission_id`` when present, else ``scope``.

    This is a *local* durable sink — the base "telemetry belongs to the runtime" position — not the
    enterprise SIEM/OTel export plane, which stays in the enterprise telemetry bridge.
    """
    from .types import to_jsonable   # noqa: PLC0415

    def _sink(ev: "Any") -> None:
        payload = to_jsonable(ev)
        mission_id = payload.get("mission_id") if isinstance(payload, dict) else ""
        store.append("RuntimeSecurityObserved", mission_id or scope, payload)

    return _sink
