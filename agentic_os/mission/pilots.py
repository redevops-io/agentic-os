"""Pilot capability operators — the first agentic apps wrapped as Mission Runtime operators.

Reference implementations for billing / support / books / compliance / crm. In production each
of these is the real `agents/<app>` service exposing `operator.router()`; here the handlers are
deterministic stand-ins so the whole mission loop runs (and tests) without the live cores. Swap a
handler for a real core call and nothing else changes.
"""
from __future__ import annotations

from .operator_sdk import Operator, capability
from .registry import CapabilityRegistry

# grants an onboarding mission runs under (least-privilege; the compiler fails closed without them)
PILOT_GRANTS = ["billing:write", "support:write", "books:write", "compliance:write", "crm:write"]


def build_pilot_operators() -> dict[str, Operator]:
    billing = Operator("agentic-billing", [
        capability("billing.create_subscription",
                   lambda i: {"subscription_id": "sub_123", "plan": "pro"},
                   provides=["subscription"], outputs={"subscription_id": "string"},
                   side_effecting=True, undo="billing.cancel_subscription",
                   permissions=["billing:write"], usd=0.01, latency_ms=800, estimated_value="high"),
        capability("billing.cancel_subscription", lambda i: {"cancelled": True},
                   side_effecting=True, permissions=["billing:write"]),
    ])
    support = Operator("agentic-support", [
        capability("support.send_onboarding",
                   lambda i: {"sent": True, "for": (i.get("subscription") or {}).get("subscription_id")},
                   provides=["onboarding_sent"], side_effecting=True,
                   permissions=["support:write"], usd=0.002, latency_ms=500),
    ])
    books = Operator("agentic-books", [
        capability("books.record_revenue",
                   lambda i: {"entry": "je_88"}, provides=["revenue_recorded"],
                   side_effecting=True, undo="books.reverse_entry",
                   permissions=["books:write"], usd=0.001, latency_ms=400),
        capability("books.reverse_entry", lambda i: {"reversed": True},
                   side_effecting=True, permissions=["books:write"]),
    ])
    compliance = Operator("agentic-compliance", [
        capability("compliance.file_consent",
                   lambda i: {"consent_id": "gdpr_42"}, provides=["consent_filed"],
                   side_effecting=True, approval_required=True,   # the human gate in onboarding
                   permissions=["compliance:write"], latency_ms=300, estimated_value="high"),
    ])
    crm = Operator("agentic-crm", [
        capability("crm.research_account",
                   lambda i: {"brief": "mid-market roofer", "signals": ["storm season"]},
                   provides=["account_brief"], permissions=["crm:read"],
                   usd=0.002, latency_ms=4000),
        capability("crm.draft_outreach",
                   lambda i: {"draft": "Hi — noticed the storm season..."},
                   provides=["outreach_draft"], permissions=["crm:read"], usd=0.003),
    ])
    return {op.name: op for op in (billing, support, books, compliance, crm)}


def build_pilot_registry(operators: dict[str, Operator] | None = None) -> CapabilityRegistry:
    operators = operators or build_pilot_operators()
    reg = CapabilityRegistry()
    for op in operators.values():
        reg.register(op.manifest)
    return reg
