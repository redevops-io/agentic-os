"""Deal-state from a real Twenty opportunity (plan §9, P5 wiring)."""
from __future__ import annotations

from agentic_os.commercial import (
    FieldReadback, reported_deal_from_opp, reported_deal_from_twenty, verified_deal_from_env,
)
from agentic_os.deal_closing.contracts import ClaimStatus


_OPP = {"id": "opp_1", "name": "Acme expansion", "stage": "negotiation",
        "amount": {"amountMicros": "4500000000000", "currencyCode": "USD"},   # 4,500,000.00 → 450,000,000 cents
        "closeDate": "2026-11-15", "probability": 0.8}


def test_reported_deal_from_opp_maps_fields():
    d = reported_deal_from_opp(_OPP, tenant="meridian")
    assert d.opportunity_ref == "opp_1" and d.reported_stage == "negotiation"
    assert d.reported_amount_cents == 450_000_000 and d.currency == "USD"
    assert d.reported_close_date == "2026-11-15" and d.reported_probability == 0.8
    assert d.source_systems == ("twenty",)


class _FakeTwenty:
    def __init__(self, opps): self._opps = opps
    def opportunities(self, *, limit=60): return self._opps


def test_reported_deal_from_twenty_picks_active():
    client = _FakeTwenty([{"id": "won1", "stage": "won"}, _OPP])
    d = reported_deal_from_twenty(client)
    assert d.opportunity_ref == "opp_1"          # skipped the terminal 'won'
    # by explicit ref
    assert reported_deal_from_twenty(client, opportunity_ref="won1").opportunity_ref == "won1"


def test_reported_deal_none_when_empty():
    assert reported_deal_from_twenty(_FakeTwenty([])) is None


def test_verified_deal_from_env_applies_readbacks():
    client = _FakeTwenty([_OPP])
    vd = verified_deal_from_env(client=client,
                                readbacks=[FieldReadback("amount_cents", 320_000_000, source="erpnext")])
    # the ERP read-back conflicts with the CRM-reported amount → CONFLICTED claim
    amount_claim = next(c for c in vd.claims if c.field_name == "amount_cents")
    assert amount_claim.status() is ClaimStatus.CONFLICTED


def test_verified_deal_none_without_client(monkeypatch):
    monkeypatch.delenv("TWENTY_BASE_URL", raising=False)
    monkeypatch.delenv("TWENTY_API_KEY", raising=False)
    assert verified_deal_from_env(client=None) is None
