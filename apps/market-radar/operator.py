"""agentic-market-radar as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive market-radar as a capability operator — the same core
changedetection.io actions the console exposes at `/agent/run`, now discoverable +
idempotent on the wire.

Capabilities (syscalls):
  radar.add_watch  — start monitoring a new competitor/price page (creates a watch)
  radar.brief      — read-only competitive-intelligence summary of what changed (fact `market_brief`)

Per modules.yaml there is NO approval gate for market-radar: add_watch is non-destructive
(it only starts observing a public page) and brief is read-only, so neither is parked as a
HumanTask. add_watch is side-effecting (it creates a watch); the runtime's idempotency-key
dedupe gives it exactly-once semantics on the wire.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_market_radar_operator() -> Operator:
    return Operator("market-radar", [
        capability(
            "radar.add_watch",
            lambda inp: core.add_watch(inp),
            provides=["watch"],
            outputs={"watch": "a new changedetection.io watch monitoring a competitor/price page"},
            side_effecting=True,
            permissions=["market-radar:write"], estimated_value="medium", latency_ms=1200,
        ),
        capability(
            "radar.brief",
            lambda inp: core.brief(inp),
            provides=["market_brief"],
            outputs={"market_brief": "read-only summary of what changed across the tracked watches"},
            estimated_value="high", deterministic=False, latency_ms=800,
        ),
    ])
