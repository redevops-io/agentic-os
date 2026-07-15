"""Observability + monitoring (v6 Phase 6.3) — read the mission-native data no one else has.

Everything here is derived from the event log the kernel already emits (`NodeDispatched` →
`NodeSucceeded`/`NodeFailed`/`NodeCompensated`, `MissionCreated` → `MissionOutcome`), so nothing
new has to be instrumented:

  * `mission_spans(rt, mid)`  → OTEL-shaped spans (a root mission span + one per node execution)
    ready to hand to an OTEL/Langfuse exporter;
  * `fleet_slos(rt)`          → the monitoring SLOs (success rate, p50/p95 latency, approval-queue
    depth, missions by state) a control-tower / Metabase dashboard renders and alerts fire on.

The actual OTEL/Langfuse push and the dashboard are thin adapters over these two — the mapping
from mission events to telemetry is the part worth testing, and it lives here.
"""
from __future__ import annotations

_END = {"NodeSucceeded": "ok", "NodeFailed": "error", "NodeCompensated": "compensated"}


def mission_spans(rt, mission_id: str) -> list[dict]:
    """A root mission span + one span per node execution, with wall-clock durations and status."""
    tl = rt.repo.timeline(mission_id)
    starts: dict[str, tuple[float, str]] = {}
    root_start = root_end = None
    spans: list[dict] = []
    for e in tl:
        t, ts, p = e["type"], e["ts"], (e.get("payload") or {})
        if t == "MissionCreated":
            root_start = ts
        elif t == "MissionOutcome":
            root_end = ts
        elif t == "NodeDispatched":
            starts[p.get("node_id")] = (ts, p.get("capability", ""))
        elif t in _END:
            nid = p.get("node_id")
            start_ts, cap = starts.pop(nid, (ts, p.get("capability", "")))
            spans.append({
                "kind": "node", "name": cap or p.get("capability", ""), "node_id": nid,
                "status": _END[t], "start": start_ts, "end": ts,
                "duration_ms": max(0, round((ts - start_ts) * 1000)),
            })
    m = rt._missions.get(mission_id)
    root = {
        "kind": "mission", "name": f"mission:{(m.template if m else None) or 'mission'}",
        "mission_id": mission_id, "status": m.state.value if m else "unknown",
        "start": root_start, "end": root_end,
        "duration_ms": (max(0, round((root_end - root_start) * 1000))
                        if root_start is not None and root_end is not None else None),
    }
    return [root] + spans


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = min(len(s) - 1, max(0, round((pct / 100.0) * (len(s) - 1))))
    return round(s[k], 1)


def fleet_slos(rt) -> dict:
    """Monitoring SLOs across every mission the runtime knows: success rate, latency percentiles,
    approval-queue depth, and a by-state breakdown (the alerting surface)."""
    rows = rt.missions()
    by_state: dict[str, int] = {}
    latencies: list[float] = []
    pending_approvals = 0
    for row in rows:
        by_state[row["state"]] = by_state.get(row["state"], 0) + 1
        if row.get("awaiting_human"):
            pending_approvals += 1
        tl = rt.repo.timeline(row["id"])
        created = next((e["ts"] for e in tl if e["type"] == "MissionCreated"), None)
        ended = next((e["ts"] for e in tl if e["type"] == "MissionOutcome"), None)
        if created is not None and ended is not None:
            latencies.append((ended - created) * 1000)
    total = len(rows)
    return {
        "missions": total,
        "by_state": by_state,
        "success_rate": round(by_state.get("succeeded", 0) / total, 3) if total else 0.0,
        "failed": by_state.get("failed", 0),
        "pending_approvals": pending_approvals,
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
    }
