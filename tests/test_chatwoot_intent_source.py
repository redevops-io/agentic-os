"""REVENUE_LEAKAGE from Chatwoot commercial intent (Revenue plan §6, §7, §21) + multi-source composition.

Unit lane: a stub Chatwoot client → a QUOTE_REQUEST conversation with no open quote surfaces as an
UNANSWERED_QUOTE_INTENT leakage through the broker; an already-quoted request is suppressed; a composite
source merges Chatwoot + another source under one scope. Live lane: opt-in, self-skips unless a real Chatwoot
is configured + reachable (`CHATWOOT_API_URL` / `CHATWOOT_API_TOKEN` / `CHATWOOT_ACCOUNT_ID`), and only READS.
"""
from __future__ import annotations

import os

import pytest

from agentic_os.integrations.chatwoot import ChatwootClient, chatwoot_from_env, scan_unanswered_quotes
from agentic_os.intelligence import resolve_decision_need
from agentic_os.intelligence.families import (
    ChatwootLeakageState, InMemoryRevenueState, chatwoot_revenue_registry, composite_revenue_registry,
    revenue_synthesize,
)
from agentic_os.revenue.leakage import LeakageType, RevenueLeakage
from runtime_contracts.protocol import Capability, DecisionNeed

_NOW = 1_774_000_000_000
_LIVE = {k: os.environ.get(k) for k in ("CHATWOOT_API_URL", "CHATWOOT_API_TOKEN", "CHATWOOT_ACCOUNT_ID")}


class _StubChatwoot(ChatwootClient):
    def __init__(self):
        super().__init__(base_url="http://stub", api_token="t", account_id="1")

    def conversations(self, *, status="open", max_pages=20):
        return [
            {"id": 11, "messages": [{"message_type": 0,
                                     "content": "Hi, please send a quote for 2 units of RIM — pricing + availability?"}],
             "meta": {"sender": {"name": "ACME Corp", "email": "buyer@acme.com"}}},
            {"id": 12, "messages": [{"message_type": 0, "content": "thanks, the invoice looks correct"}],
             "meta": {"sender": {"name": "Beta LLC"}}},
        ]


def _need(subject, cap=Capability.REVENUE_LEAKAGE, **kw):
    base = dict(decision_case_id="dc1", capability=cap, question="what revenue is leaking?",
                objective="revenue_review", subject_refs=(subject,), tenant="t", min_confidence=0.0,
                as_of="2026-03-20T00:00:00Z", known_at="2026-03-20T00:00:00Z")
    base.update(kw)
    return DecisionNeed(**base)


# ── unit (stub client) ──────────────────────────────────────────────────────────────────────────────────
def test_scan_surfaces_quote_request_only():
    leaks = scan_unanswered_quotes(_StubChatwoot(), now_ms=_NOW)
    assert len(leaks) == 1 and leaks[0].leakage_type is LeakageType.UNANSWERED_QUOTE_INTENT
    assert leaks[0].subject == "ACME Corp"


def test_chatwoot_leakage_resolves_through_the_broker():
    reg = chatwoot_revenue_registry(_StubChatwoot(), scope_ref="book:acme", now_ms=_NOW)
    res, _ = resolve_decision_need(reg, _need("book:acme"), synthesize=revenue_synthesize)
    assert res.metrics["count"] == 1 and "UNANSWERED_QUOTE_INTENT" in res.answer
    assert res.total_cost == 0.0 and res.provider_receipts[0].provider == "internal.revenue_intelligence"


def test_already_quoted_request_is_suppressed():
    reg = chatwoot_revenue_registry(_StubChatwoot(), scope_ref="book:acme", now_ms=_NOW,
                                    has_open_quote=lambda _s: True)
    res, _ = resolve_decision_need(reg, _need("book:acme"), synthesize=revenue_synthesize)
    assert res.metrics["count"] == 0 and "No recoverable revenue leakage" in res.answer


def test_other_subject_is_no_match():
    reg = chatwoot_revenue_registry(_StubChatwoot(), scope_ref="book:acme", now_ms=_NOW)
    res, _ = resolve_decision_need(reg, _need("book:other"), synthesize=revenue_synthesize)
    assert res.answer == ""


# ── composite (merge Chatwoot + another source under one scope) ──────────────────────────────────────────
def test_composite_merges_leakage_across_sources():
    twenty_like = InMemoryRevenueState()
    twenty_like.set_leakages("book:acme", [RevenueLeakage(
        LeakageType.STALLED_OPPORTUNITY, subject="ACME expansion", expected_value=0.6, confidence=0.9,
        urgency=0.5, proposed_action="Re-engage", required_capability="crm.opportunity.followup",
        amount_cents=3_000_000)])
    chatwoot = ChatwootLeakageState(_StubChatwoot(), scope_ref="book:acme", now_ms=_NOW)
    reg = composite_revenue_registry([twenty_like, chatwoot])
    res, _ = resolve_decision_need(reg, _need("book:acme"), synthesize=revenue_synthesize)
    assert res.metrics["count"] == 2                       # 1 stalled opp + 1 unanswered quote
    kinds = {l["leakage_type"] for l in res.metrics["leakages"]}
    assert kinds == {"STALLED_OPPORTUNITY", "UNANSWERED_QUOTE_INTENT"}


# ── live (opt-in; READ-ONLY; self-skips) ────────────────────────────────────────────────────────────────
def test_live_chatwoot_leakage(monkeypatch):
    if not (_LIVE["CHATWOOT_API_URL"] and _LIVE["CHATWOOT_API_TOKEN"]):
        pytest.skip("no ambient Chatwoot creds (CHATWOOT_API_URL / CHATWOOT_API_TOKEN)")
    for k, v in _LIVE.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    client = chatwoot_from_env()
    if client is None or not client.connected():
        pytest.skip("Chatwoot configured but not reachable/authorized")
    import time
    reg = chatwoot_revenue_registry(client, scope_ref="book:live", now_ms=int(time.time() * 1000))
    res, _ = resolve_decision_need(reg, _need("book:live"), synthesize=revenue_synthesize)
    assert res.total_cost == 0.0                           # internal provider — read-only, cost-0
    assert res.metrics.get("count") is not None            # a real (possibly empty) scan resolved
