"""agentic-growth-engine as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive growth as a capability operator — the same core Umami
attribution actions the console exposes at `/agent/run`, now discoverable + idempotent
on the wire.

Capabilities (syscalls):
  growth.analyze            — read-only lead-source attribution + a shift recommendation
  growth.reallocate_budget  — stage an ad-budget change      [approval gate: budget_change]

The budget-moving capability carries approval_required=True (matches modules.yaml's
`approval_required: [budget_change]` gate), so the runtime parks it as a HumanTask before
execution — ad spend lives in the external Ads platform. analyze is a read, not gated.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_growth_operator() -> Operator:
    return Operator("growth-engine", [
        capability(
            "growth.analyze",
            lambda inp: core.analyze(),
            provides=["growth_attribution"],
            outputs={"growth_attribution": "lead-source attribution + shift recommendation from the live Umami core"},
            estimated_value="medium", deterministic=False, latency_ms=400,
            concurrency_mode="read_only",
        ),
        capability(
            "growth.reallocate_budget",
            lambda inp: core.reallocate_budget(inp),
            provides=["budget_change_staged"],
            outputs={"budget_change_staged": "ad-budget shift staged for human approval (budget_change gate)"},
            side_effecting=True, approval_required=True,
            permissions=["growth:write"], estimated_value="high", latency_ms=500,
            # money-moving budget changes serialize on a single ad-budget resource — never two concurrent
            # reallocations racing the same spend envelope (already approval-gated on top).
            concurrency_mode="exclusive", resource_keys=["growth:ad-budget"],
        ),
    ])
