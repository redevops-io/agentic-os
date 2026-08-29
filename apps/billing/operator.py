"""agentic-billing as a Mission Runtime operator (Phase-1 production wiring).

Mounts the Operator SDK surface (`GET /capabilities` + `POST /invoke`) onto the app so
the Mission Runtime can drive billing as a capability operator — the same core Lago
actions the console exposes at `/agent/run`, now discoverable + idempotent on the wire.

Capabilities (syscalls):
  billing.summary              — read-only KPIs + overdue invoices (fact `billing_activity`)
  billing.dunning              — retry payment across overdue invoices  [approval gate: dunning]
  billing.refund               — stage a refund credit-note              [approval gate: refund]
  billing.create_subscription  — onboard a customer onto a paid plan (undo: cancel_subscription)
  billing.cancel_subscription  — terminate a subscription (saga compensation)

Money-moving capabilities carry approval_required=True (matches modules.yaml gates), so the
runtime parks them as HumanTasks before execution.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability

from . import core


def build_billing_operator() -> Operator:
    return Operator("billing", [
        capability(
            "billing.summary",
            lambda inp: core.fetch_activity(),
            provides=["billing_activity"],
            outputs={"billing_activity": "KPIs + overdue invoices from the live Lago core"},
            estimated_value="low", deterministic=False, latency_ms=300,
            concurrency_mode="read_only",
        ),
        capability(
            "billing.dunning",
            lambda inp: core.chase_overdue(),
            provides=["dunning_attempted"],
            outputs={"dunning_attempted": "retry_payment issued across all overdue invoices"},
            side_effecting=True, approval_required=True,
            permissions=["billing:write"], estimated_value="high", latency_ms=1500,
            concurrency_mode="exclusive", resource_keys=["billing:dunning"],  # one dunning run at a time
        ),
        capability(
            "billing.refund",
            lambda inp: core.stage_refund(inp),
            provides=["refund_staged"],
            outputs={"refund_staged": "refund credit-note staged for human approval"},
            side_effecting=True, approval_required=True,
            permissions=["billing:write"], estimated_value="medium", latency_ms=500,
            concurrency_mode="exclusive", concurrency_key="billing:invoice:{invoice}",
        ),
        capability(
            "billing.create_subscription",
            lambda inp: core.create_subscription(inp),
            provides=["subscription"],
            outputs={"subscription": "paid subscription created on the live Lago core"},
            side_effecting=True, undo="billing.cancel_subscription",
            permissions=["billing:write"], estimated_value="high", latency_ms=1200,
            concurrency_mode="exclusive", concurrency_key="billing:customer:{customer}",
        ),
        capability(
            "billing.cancel_subscription",
            lambda inp: core.cancel_subscription(inp),
            provides=["subscription_cancelled"],
            outputs={"subscription_cancelled": "subscription terminated (saga compensation)"},
            side_effecting=True,
            permissions=["billing:write"], estimated_value="medium", latency_ms=800,
            concurrency_mode="exclusive", concurrency_key="billing:customer:{customer}",
        ),
    ])
