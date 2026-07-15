"""agentic-outreach-engine as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so the
Mission Runtime can drive outreach as a capability operator — the same core Twenty CRM
actions the console exposes at `/agent/run`, now discoverable + idempotent on the wire.

Capabilities (syscalls):
  outreach.refresh    — rebuild the ranked pipeline from live GitHub/HN signals (no CRM write)
  outreach.approve    — human sign-off that syncs a real pilot to Twenty (Company+Person+Opportunity)
  outreach.send_all   — dispatch the approved outreach sequences to prospects  [approval gate: send_sequence]

Per control-plane modules.yaml the gate is `approval_required: [send_sequence]` — nothing reaches a
prospect without a human sign-off — so `outreach.send_all` carries approval_required=True and the
runtime parks it as a HumanTask before execution.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_outreach_operator() -> Operator:
    return Operator("outreach-engine", [
        capability(
            "outreach.refresh",
            lambda inp: core.refresh(inp),
            provides=["outreach_pipeline"],
            outputs={"outreach_pipeline": "ranked accounts + drafted openers rebuilt from live signals"},
            estimated_value="low", deterministic=False, latency_ms=1200,
        ),
        capability(
            "outreach.approve",
            lambda inp: core.approve(inp),
            provides=["lead_synced"],
            outputs={"lead_synced": "approved lead synced to Twenty (Company + Person + Opportunity)"},
            side_effecting=True,
            permissions=["outreach:write"], estimated_value="high", latency_ms=1500,
        ),
        capability(
            "outreach.send_all",
            lambda inp: core.send_all(inp),
            provides=["sequences_sent"],
            outputs={"sequences_sent": "approved outreach sequences dispatched to prospects"},
            side_effecting=True, approval_required=True,
            permissions=["outreach:write"], estimated_value="high", latency_ms=800,
        ),
    ])
