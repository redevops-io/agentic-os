"""REVENUE_LEAKAGE from Lago usage-vs-plan (Revenue plan §6, §21).

Unit lane: a stub Lago client → a subscription whose metered usage approaches the plan boundary surfaces as an
EXPANSION_OPPORTUNITY through the broker; a low-usage or unhealthy account does not. Live lane: opt-in,
self-skips unless a real Lago is configured + reachable (`LAGO_API_URL` / `LAGO_API_KEY`), and only READS.
"""
from __future__ import annotations

import os

import pytest

from agentic_os.integrations.lago import LagoClient, lago_from_env, scan_expansion
from agentic_os.intelligence import resolve_decision_need
from agentic_os.intelligence.families import (
    LagoExpansionState, lago_revenue_registry, revenue_synthesize,
)
from agentic_os.revenue.leakage import LeakageType
from runtime_contracts.protocol import Capability, DecisionNeed

_LIVE = {k: os.environ.get(k) for k in ("LAGO_API_URL", "LAGO_API_KEY")}


class _StubLago(LagoClient):
    """A LagoClient with canned subscriptions / plans / usage (no network). GLOBEX is at 84% of plan; BETA
    at 20%."""
    def __init__(self):
        super().__init__(base_url="http://stub", api_key="k")

    def subscriptions(self, *, status="active", max_pages=20):
        return [{"external_customer_id": "GLOBEX", "external_id": "sub_globex", "plan_code": "growth"},
                {"external_customer_id": "BETA", "external_id": "sub_beta", "plan_code": "growth"}]

    def plan(self, code):
        return {"amount_cents": 29_900} if code == "growth" else {}

    def current_usage(self, external_customer_id, external_subscription_id):
        return {"total_amount_cents": 25_000 if external_customer_id == "GLOBEX" else 6_000}


def _need(subject, cap=Capability.REVENUE_LEAKAGE, **kw):
    base = dict(decision_case_id="dc1", capability=cap, question="what revenue is leaking?",
                objective="revenue_review", subject_refs=(subject,), tenant="t", min_confidence=0.0,
                as_of="2026-03-20T00:00:00Z", known_at="2026-03-20T00:00:00Z")
    base.update(kw)
    return DecisionNeed(**base)


# ── unit (stub client) ──────────────────────────────────────────────────────────────────────────────────
def test_scan_flags_only_the_near_boundary_account():
    leaks = scan_expansion(_StubLago())          # GLOBEX 25000/29900 = 0.84 >= 0.8; BETA 0.20 < 0.8
    assert len(leaks) == 1 and leaks[0].leakage_type is LeakageType.EXPANSION_OPPORTUNITY
    assert leaks[0].subject == "GLOBEX"


def test_expansion_resolves_through_the_broker():
    reg = lago_revenue_registry(_StubLago(), scope_ref="book:acme")
    res, _ = resolve_decision_need(reg, _need("book:acme"), synthesize=revenue_synthesize)
    assert res.metrics["count"] == 1 and "EXPANSION_OPPORTUNITY" in res.answer
    assert res.total_cost == 0.0 and res.provider_receipts[0].provider == "internal.revenue_intelligence"


def test_unhealthy_account_is_not_flagged():
    reg = lago_revenue_registry(_StubLago(), scope_ref="book:acme", account_healthy=lambda _s: False)
    res, _ = resolve_decision_need(reg, _need("book:acme"), synthesize=revenue_synthesize)
    assert res.metrics["count"] == 0 and "No recoverable revenue leakage" in res.answer


def test_other_subject_is_no_match():
    reg = lago_revenue_registry(_StubLago(), scope_ref="book:acme")
    res, _ = resolve_decision_need(reg, _need("book:other"), synthesize=revenue_synthesize)
    assert res.answer == ""


# ── live (opt-in; READ-ONLY; self-skips) ────────────────────────────────────────────────────────────────
def test_live_lago_expansion(monkeypatch):
    if not all(_LIVE.values()):
        pytest.skip("no ambient Lago creds (LAGO_API_URL / LAGO_API_KEY)")
    for k, v in _LIVE.items():
        monkeypatch.setenv(k, v)
    client = lago_from_env()
    if client is None or not client.connected():
        pytest.skip("Lago configured but not reachable")
    reg = lago_revenue_registry(client, scope_ref="book:live")
    res, _ = resolve_decision_need(reg, _need("book:live"), synthesize=revenue_synthesize)
    assert res.total_cost == 0.0                           # internal provider — read-only, cost-0
    assert res.metrics.get("count") is not None            # a real (possibly empty) scan resolved
