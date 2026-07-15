"""agentic-social-autopilot as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so the
Mission Runtime can drive social-autopilot as a capability operator — the same core Postiz
actions the console exposes at `/agent/run`, now discoverable + idempotent on the wire.

Capabilities (syscalls):
  social.draft    — generate post copy + stage it as a DRAFT in Postiz (real postgres write)
  social.publish  — publish a post to the social networks       [approval gate: publish]

Publishing is the one action that pushes content OUT to the public, so social.publish carries
approval_required=True (matches modules.yaml `approval_required:[publish]`); the runtime parks
it as a HumanTask before execution. Drafting only persists a private DRAFT to Postiz, so it is
side_effecting but not gated.

Heavy content-generation surfaces (the vibexgen trends/generate adapter and the host-side UI
"demo reel" recorder) are intentionally NOT capabilities: they depend on external GPU/browser
workers and are not pure Postiz-core actions. They stay in app.py as UI-only endpoints.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_social_operator() -> Operator:
    return Operator("social-autopilot", [
        capability(
            "social.draft",
            lambda inp: core.draft(inp),
            provides=["draft_staged"],
            outputs={"draft_staged": "post copy generated and staged as a DRAFT in Postiz"},
            side_effecting=True,
            permissions=["social:write"], estimated_value="medium", latency_ms=1200,
        ),
        capability(
            "social.publish",
            lambda inp: core.publish(inp),
            provides=["publish_staged"],
            outputs={"publish_staged": "post staged for human approval before it goes live"},
            side_effecting=True, approval_required=True,
            permissions=["social:write"], estimated_value="high", latency_ms=800,
        ),
    ])
