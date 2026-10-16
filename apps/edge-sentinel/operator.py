"""edge-sentinel as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive edge-sentinel as a capability operator — the same core
CrowdSec actions the SOC console exposes at `/agent/run`, now discoverable + idempotent
on the wire.

Capabilities (syscalls):
  sentinel.triage      — read-only threat posture: active decisions + alerts + severity mix
  sentinel.block_ip    — enforce a ban decision at the edge   [approval gate: remediation]
                         (PRESERVE on failure; lifting it = sentinel.unblock_ip, a human decision)
  sentinel.unblock_ip  — lift a ban decision (saga compensation)  [approval gate]

Per modules.yaml the gate is `approval_required:[remediation]` — writing a firewall
ban/decision (block_ip) carries approval_required=True + side_effecting=True, so the
runtime parks it as a HumanTask before execution. triage (reads alerts + explains) is
read-only and ungated.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability, pending_approval

from . import core


def build_edge_sentinel_operator() -> Operator:
    return Operator("edge-sentinel", [
        capability(
            "sentinel.triage",
            lambda inp: core.triage(),
            provides=["threat_posture"],
            outputs={"threat_posture": "active decisions + alerts triage from the live CrowdSec core"},
            estimated_value="medium", deterministic=False, latency_ms=400,
            concurrency_mode="read_only",
        ),
        capability(
            "sentinel.block_ip",
            lambda inp: (core.block_ip(inp) if inp.get("_approval")
                        else pending_approval("sentinel.block_ip", ip=str(inp.get("ip") or "").strip(), ip_blocked=False)),
            provides=["ip_blocked"],
            outputs={"ip_blocked": "True ONLY if a read-back of the LAPI decisions for the IP (parsed IP equality) shows an active ban; a 2xx POST alone is not enough"},
            side_effecting=True,
            compensation={"supported": False, "disposition": "PRESERVE",
                          "notes": "a completed security block is NOT auto-reversed by a failed workflow (the safer state "
                                   "is kept); lifting it (sentinel.unblock_ip) is a separate, approved human decision"}, approval_required=True,
            permissions=["sentinel:write"], estimated_value="high", latency_ms=1200,
            concurrency_mode="exclusive", concurrency_key="sentinel:ip:{ip}",
        ),
        capability(
            "sentinel.unblock_ip",
            lambda inp: (core.unblock_ip(inp) if inp.get("_approval")
                        else pending_approval("sentinel.unblock_ip", ip=str(inp.get("ip") or "").strip(), ip_unblocked=False)),
            provides=["ip_unblocked"],
            outputs={"ip_unblocked": "ban decision lifted (saga compensation)"},
            side_effecting=True,
            compensation={"supported": False, "disposition": "PRESERVE", "notes": "lifting a security block is verified by read-back; re-blocking is a new forward action"}, approval_required=True,
            permissions=["sentinel:write"], estimated_value="medium", latency_ms=800,
            concurrency_mode="exclusive", concurrency_key="sentinel:ip:{ip}",
        ),
    ])
