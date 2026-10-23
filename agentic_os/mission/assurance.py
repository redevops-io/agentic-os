"""Agent-assurance sensors over the mission event log (read-only).

Pure functions: given a mission's events (and a clock) they report the situations an operator, or a monitor such
as Edge Sentinel, should look at BEFORE a customer does. They never change anything and never decide an action;
an alert is not closure (closing needs a verified terminal state).

Sensor catalog (``kind``):
  stuck_gate               a human gate has been waiting longer than ``gate_after_s``
  dispatch_without_result  a node was dispatched, nothing came back, for longer than ``dispatch_after_s``
                           (the effect MAY exist: reconcile before any retry)
  repeated_failures        the same capability failed ``failures`` or more times inside ``window_s``
  approval_invalidated     an approval stopped applying because the inputs changed after the human saw them
  approval_expired         an approval was refused because the gate lapsed
  unverified_success       a side-effecting node succeeded with no recorded verification
  ledger_tampered          the event log's hash chain does not verify
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List

SEVERITY = {"ledger_tampered": "critical", "dispatch_without_result": "high", "approval_invalidated": "high",
            "unverified_success": "high", "repeated_failures": "medium", "stuck_gate": "medium",
            "approval_expired": "low"}


@dataclass(frozen=True)
class AssuranceFinding:
    kind: str
    severity: str
    mission_id: str
    node_id: str
    detail: str
    seq: int = 0            # the event the finding points at (evidence)


def assurance_findings(events: Iterable[Any], *, now: float, gate_after_s: float = 86_400.0,
                       dispatch_after_s: float = 900.0, failures: int = 3, window_s: float = 3_600.0,
                       side_effecting: "set[str] | None" = None) -> List[AssuranceFinding]:
    """Findings for ``events`` (any mix of missions). ``side_effecting`` names capabilities whose success must have
    a recorded verification; when omitted, every NodeSucceeded is held to it."""
    from .store import verify_chain  # noqa: PLC0415
    evs = list(events)
    out: List[AssuranceFinding] = []

    def add(kind: str, ev: Any, node: str, detail: str) -> None:
        out.append(AssuranceFinding(kind, SEVERITY[kind], ev.mission_id, node, detail, ev.seq))

    chain = verify_chain(evs)
    if not chain.ok:
        bad = next((e for e in evs if e.seq == chain.first_bad_seq), evs[0])
        add("ledger_tampered", bad, "", chain.reason)

    by_mission: dict[str, list] = {}
    for e in evs:
        by_mission.setdefault(e.mission_id, []).append(e)

    for mid, mevs in by_mission.items():
        parked: dict[str, Any] = {}
        dispatched: dict[str, Any] = {}
        verified_after: set[str] = set()
        failed_at: dict[str, list] = {}
        for e in mevs:
            p = e.payload or {}
            node = str(p.get("node_id", ""))
            if e.type == "NodeParked":
                parked[node] = e
            elif e.type in ("ApprovalGranted", "ApprovalRejected", "NodeResumed", "NodeSkipped"):
                parked.pop(node, None)
            elif e.type == "NodeDispatched":
                dispatched[node] = e
            elif e.type in ("NodeSucceeded", "NodeFailed"):
                dispatched.pop(node, None)
                if e.type == "NodeFailed":
                    failed_at.setdefault(str(p.get("capability", node)), []).append(e)
                elif side_effecting is None or p.get("capability") in side_effecting:
                    if node not in verified_after:
                        add("unverified_success", e, node, f"{p.get('capability', node)} succeeded with no "
                                                           "recorded verification before it")
            elif e.type == "VerificationRecorded":
                verified_after.add(str(p.get("node_id", "")))
            elif e.type == "ApprovalInvalidated":
                add("approval_invalidated", e, node, str(p.get("reason", "")))
            elif e.type == "ApprovalExpired":
                add("approval_expired", e, node, f"gate lapsed after {p.get('ttl_s')}s")
        for node, e in parked.items():
            if now - e.ts > gate_after_s:
                add("stuck_gate", e, node, f"waiting for a human for {int((now - e.ts) // 60)} min")
        for node, e in dispatched.items():
            if now - e.ts > dispatch_after_s:
                add("dispatch_without_result", e, node,
                    "dispatched with no result: the effect may exist; reconcile before retrying")
        for cap, fs in failed_at.items():
            recent = [f for f in fs if now - f.ts <= window_s]
            if len(recent) >= failures:
                add("repeated_failures", recent[-1], str(recent[-1].payload.get("node_id", "")),
                    f"{cap} failed {len(recent)} times in {int(window_s // 60)} min")
    return out
