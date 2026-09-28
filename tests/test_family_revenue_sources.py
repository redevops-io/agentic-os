"""Live sources for the Revenue broker — Twenty CRM binding (Revenue & Execution plan §2, §6).

Unit lane: a stub Twenty client → the STALLED_OPPORTUNITY leakage resolves through the broker as a governed
IntelligenceResult, scoped to one book ref (any other subject is NO_MATCH). Live lane: opt-in, self-skips
unless a real Twenty is configured + reachable (`TWENTY_BASE_URL` + `TWENTY_API_KEY`), and only READS.
"""
from __future__ import annotations

import os

import pytest

from agentic_os.intelligence import resolve_decision_need
from agentic_os.intelligence.families import (
    TwentyLeakageState, revenue_synthesize, twenty_revenue_registry,
)
from agentic_os.integrations.twenty import TwentyClient, twenty_from_env
from runtime_contracts.protocol import Capability, DecisionNeed

_NOW = 1_774_000_000_000
_DAY = 86_400_000

# Snapshot any ambient Twenty creds at import time — the autouse `_no_live_cores` fixture (conftest) wipes
# them per-test for hermeticity, so the live lane re-applies them explicitly (the repo's opt-in convention).
_LIVE_TWENTY = {k: os.environ.get(k) for k in ("TWENTY_BASE_URL", "TWENTY_API_KEY")}


class _StubTwenty(TwentyClient):
    """A TwentyClient whose HTTP is replaced by a canned opportunity list (one stalled, one fresh)."""
    def __init__(self):
        super().__init__(base_url="http://stub", api_key="x")

    def opportunities(self, *, limit: int = 60) -> list:
        stale_at = _iso(_NOW - 40 * _DAY)
        fresh_at = _iso(_NOW - 2 * _DAY)
        return [
            {"id": "o1", "name": "ACME expansion", "stage": "proposal",
             "amount": {"amountMicros": 32_000_000_000, "currencyCode": "USD"}, "updatedAt": stale_at},
            {"id": "o2", "name": "Beta renewal", "stage": "negotiation",
             "amount": {"amountMicros": 5_000_000_000, "currencyCode": "USD"}, "updatedAt": fresh_at},
        ]

    def companies(self, *, limit: int = 60) -> list:
        return []


def _iso(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _need(cap, subject, **kw):
    base = dict(decision_case_id="dc1", capability=cap, question="what revenue is leaking?",
                objective="revenue_review", subject_refs=(subject,), tenant="t", min_confidence=0.0,
                as_of="2026-03-20T00:00:00Z", known_at="2026-03-20T00:00:00Z")
    base.update(kw)
    return DecisionNeed(**base)


# ── unit (stub client, no network) ──────────────────────────────────────────────────────────────────────
def test_twenty_leakage_resolves_through_the_broker():
    reg = twenty_revenue_registry(_StubTwenty(), scope_ref="book:acme", now_ms=_NOW)
    res, _ = resolve_decision_need(reg, _need(Capability.REVENUE_LEAKAGE, "book:acme"),
                                   synthesize=revenue_synthesize)
    assert res.metrics["count"] == 1                       # only the 40-day-stale opp, not the fresh one
    assert "STALLED_OPPORTUNITY" in res.answer and "ACME expansion" in res.answer
    assert res.total_cost == 0.0
    assert res.provider_receipts[0].provider == "internal.revenue_intelligence"


def test_scan_runs_once_and_is_cached():
    src = TwentyLeakageState(_StubTwenty(), scope_ref="book:acme", now_ms=_NOW)
    first = src.leakages("book:acme")
    assert src._scan is not None and src.leakages("book:acme") == first   # second call reuses the scan


def test_other_subject_is_no_match():
    reg = twenty_revenue_registry(_StubTwenty(), scope_ref="book:acme", now_ms=_NOW)
    res, _ = resolve_decision_need(reg, _need(Capability.REVENUE_LEAKAGE, "book:other"),
                                   synthesize=revenue_synthesize)
    assert res.answer == "" and "no evidence acquired" in res.unresolved_gaps


def test_quote_feasibility_finds_no_twenty_data():
    # Twenty carries no catalog, so a QUOTE_FEASIBILITY need against a Twenty-only source is a clean miss.
    reg = twenty_revenue_registry(_StubTwenty(), scope_ref="book:acme", now_ms=_NOW)
    res, _ = resolve_decision_need(reg, _need(Capability.QUOTE_FEASIBILITY, "book:acme"),
                                   synthesize=revenue_synthesize)
    assert res.answer == ""


# ── live (opt-in; READ-ONLY; self-skips) ────────────────────────────────────────────────────────────────
def test_live_twenty_revenue_leakage(monkeypatch):
    if not all(_LIVE_TWENTY.values()):
        pytest.skip("no ambient Twenty creds (set TWENTY_BASE_URL / TWENTY_API_KEY)")
    for k, v in _LIVE_TWENTY.items():
        monkeypatch.setenv(k, v)                           # opt back in past the hermetic env wipe
    client = twenty_from_env()
    if client is None or not client.opportunities(limit=1):
        pytest.skip("Twenty configured but not reachable/authorized")
    import time
    scope = os.environ.get("TWENTY_SCOPE_REF", "book:live")
    reg = twenty_revenue_registry(client, scope_ref=scope, now_ms=int(time.time() * 1000))
    res, trace = resolve_decision_need(reg, _need(Capability.REVENUE_LEAKAGE, scope),
                                       synthesize=revenue_synthesize)
    assert res.total_cost == 0.0                           # internal provider — read-only, cost-0
    assert res.metrics.get("count") is not None            # a real (possibly empty) scan resolved
