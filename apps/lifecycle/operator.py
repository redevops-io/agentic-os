"""lifecycle as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so the
Mission Runtime can drive lifecycle marketing as a capability operator — the same core
Listmonk actions the console exposes at `/agent/run`, now discoverable + idempotent on the
wire.

Capabilities (syscalls):
  lifecycle.compose_campaign — write a campaign DRAFT into Listmonk   [side effect: a write]
  lifecycle.segment          — propose a Listmonk SQL subscriber segment (advisory, read-only)
  lifecycle.suggest_flow     — outline a multi-step lifecycle flow     (advisory, read-only)

Gate (matches modules.yaml `approval_required: [send]`): the gated action is *send*, which
reaches real contacts. None of these three send — compose_campaign only creates a DRAFT
(status "draft"; a human reviews + sends in Listmonk), so it is marked side_effecting=True
(a real write to the core) but approval_required=False. segment/suggest_flow are advisory
with no Listmonk write.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_lifecycle_operator() -> Operator:
    return Operator("lifecycle", [
        capability(
            "lifecycle.compose_campaign",
            lambda inp: core.compose_campaign(inp),
            provides=["campaign_drafted"],
            outputs={"campaign_drafted": "a Listmonk campaign DRAFT created for human review + send"},
            side_effecting=True, approval_required=False,
            permissions=["lifecycle:write"], estimated_value="high", deterministic=False, latency_ms=1200,
            concurrency_key="provider:listmonk", max_parallelism=2,  # independent DRAFT campaigns, bounded
        ),
        capability(
            "lifecycle.segment",
            lambda inp: core.segment(inp),
            provides=["segment_proposed"],
            outputs={"segment_proposed": "a Listmonk advanced SQL subscriber query (advisory)"},
            estimated_value="medium", deterministic=False, latency_ms=600,
            concurrency_mode="read_only",
        ),
        capability(
            "lifecycle.suggest_flow",
            lambda inp: core.suggest_flow(inp),
            provides=["flow_suggested"],
            outputs={"flow_suggested": "a multi-step lifecycle flow outline with per-step copy (advisory)"},
            estimated_value="medium", deterministic=False, latency_ms=800,
            concurrency_mode="read_only",
        ),
    ])
