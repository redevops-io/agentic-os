"""Revenue Intelligence through the broker (Revenue & Execution plan §6, §8). Offline.

A DecisionNeed for QUOTE_FEASIBILITY / REVENUE_LEAKAGE resolves through the same resolve_decision_need path
to the cost-0 internal revenue provider (family INTERNAL_COMPUTED, receipted, replayable), and the revenue
synthesizer states the business answer. An unknown subject is a clean NO_MATCH; a known subject with no
leakage is a real "clean" answer, not a miss.
"""
from __future__ import annotations

import types

from agentic_os.intelligence import resolve_decision_need
from agentic_os.intelligence.families import (
    InMemoryRevenueState, QuoteInputs, RevenueIntelligenceProvider, revenue_registry, revenue_synthesize,
)
from agentic_os.revenue.leakage import stalled_opportunity
from agentic_os.revenue.quote import CatalogItem, QuoteLine
from runtime_contracts.protocol import Capability, DecisionNeed, ProviderFamily

_NOW = 1_774_000_000_000


def _catalog():
    return {
        "rim": CatalogItem("rim", "Rim", list_price_cents=40_000, unit_cost_cents=24_000, on_hand_qty=50.0),
        "hub": CatalogItem("hub", "Hub", list_price_cents=60_000, unit_cost_cents=39_000, on_hand_qty=0.0,
                           lead_time_days=10),
    }


def _state(*, with_leak=True, thin_margin=False):
    st = InMemoryRevenueState()
    cat = _catalog()
    if thin_margin:
        cat["rim"] = CatalogItem("rim", "Rim", list_price_cents=40_000, unit_cost_cents=38_000, on_hand_qty=50.0)
    st.add_quote("cust:acme", QuoteInputs(
        lines=[QuoteLine("rim", 10.0), QuoteLine("hub", 2.0)], catalog=cat, now_ms=_NOW))
    opp = types.SimpleNamespace(name="ACME expansion", stage="proposal", amount_cents=3_200_000, prov=None)
    leak = stalled_opportunity(opp, last_activity_at_ms=_NOW - 40 * 86_400_000, has_future_activity=False,
                               now_ms=_NOW)
    st.set_leakages("cust:acme", [leak] if (with_leak and leak) else [])
    return st


def _need(cap, subject="cust:acme", **kw):
    base = dict(decision_case_id="dc1", capability=cap, question="revenue?", objective="revenue_review",
                subject_refs=(subject,), tenant="t", min_confidence=0.0, as_of="2026-03-20T00:00:00Z",
                known_at="2026-03-20T00:00:00Z")
    base.update(kw)
    return DecisionNeed(**base)


# ── internal provider shape ─────────────────────────────────────────────────────────────────────────────
def test_internal_revenue_provider_is_cost_zero_and_internal_family():
    p = RevenueIntelligenceProvider(_state())
    assert p.family is ProviderFamily.INTERNAL_COMPUTED
    assert p.capabilities() == (Capability.QUOTE_FEASIBILITY, Capability.REVENUE_LEAKAGE)
    assert p.estimate_cost(_need(Capability.QUOTE_FEASIBILITY).to_evidence_request()).money == 0.0
    assert p.check_entitlement("t", Capability.REVENUE_LEAKAGE) is True


# ── quote feasibility ───────────────────────────────────────────────────────────────────────────────────
def test_quote_feasibility_resolves_through_the_broker():
    reg = revenue_registry(_state())
    res, _ = resolve_decision_need(reg, _need(Capability.QUOTE_FEASIBILITY), synthesize=revenue_synthesize)
    assert "Quote feasible" in res.answer and "blended margin" in res.answer
    assert res.total_cost == 0.0
    assert res.provider_receipts[0].provider == "internal.revenue_intelligence"
    # rim 10*$400 in stock + hub 2*$600 on a 10d lead → feasible, total $5,200
    assert res.metrics["total_cents"] == 520_000 and res.metrics["feasible"] is True


def test_thin_margin_surfaces_an_approval_requirement_as_a_gap():
    reg = revenue_registry(_state(thin_margin=True))
    res, _ = resolve_decision_need(reg, _need(Capability.QUOTE_FEASIBILITY), synthesize=revenue_synthesize)
    assert any("approval requirement" in g for g in res.unresolved_gaps)


# ── revenue leakage ─────────────────────────────────────────────────────────────────────────────────────
def test_leakage_scan_resolves_through_the_broker():
    reg = revenue_registry(_state())
    res, _ = resolve_decision_need(reg, _need(Capability.REVENUE_LEAKAGE), synthesize=revenue_synthesize)
    assert "recoverable leakage signal" in res.answer and "STALLED_OPPORTUNITY" in res.answer
    assert res.metrics["count"] == 1 and res.metrics["total_recoverable_cents"] == 3_200_000


def test_clean_scan_is_a_confident_answer_not_a_miss():
    reg = revenue_registry(_state(with_leak=False))
    res, _ = resolve_decision_need(reg, _need(Capability.REVENUE_LEAKAGE), synthesize=revenue_synthesize)
    assert "No recoverable revenue leakage" in res.answer
    assert res.metrics["count"] == 0 and res.confidence == 1.0


def test_unknown_subject_is_no_match():
    reg = revenue_registry(_state())
    res, _ = resolve_decision_need(reg, _need(Capability.REVENUE_LEAKAGE, subject="cust:nobody"),
                                   synthesize=revenue_synthesize)
    assert res.answer == "" and "no evidence acquired" in res.unresolved_gaps
