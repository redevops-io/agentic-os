"""Metering, budget and provenance for the enrichment providers (world.enrichment_metering)."""
from __future__ import annotations

import sqlite3

import pytest

from agentic_os.world import enrichment as E
from agentic_os.world.enrichment_metering import (
    LIST_PRICE, LIST_PRICE_ON_ERROR, NOT_CALLED, UNKNOWN, BudgetExceeded, EnrichmentLedger, MeteredProvider,
    prices_from_env,
)


class Fake:
    name = "hunter"

    def __init__(self, email="dana@acme.com", fail=False):
        self.calls, self._email, self._fail = [], email, fail

    def configured(self):
        return True

    def find_email(self, *, domain, first_name, last_name):
        self.calls.append("find_email")
        if self._fail:
            raise E.EnrichmentError("https://api.hunter.io/v2/email-finder -> HTTP 500")
        return {"email": self._email, "confidence": 0.9, "verified": None}

    def verify_email(self, email):
        self.calls.append("verify_email")
        return {"status": "valid", "score": 0.95, "verified": True}


def _m(fake=None, **kw):
    ledger = EnrichmentLedger()
    return MeteredProvider(fake or Fake(), ledger, tenant="t1", **kw), ledger


def test_call_is_metered_at_the_configured_price_with_provenance():
    m, ledger = _m(prices={"hunter.find_email": 49000})
    r = m.find_email(domain="Acme.com", first_name="Dana", last_name="Rivera")
    row = ledger.entries("t1")[0]
    assert (row["provider"], row["operation"], row["outcome"]) == ("hunter", "find_email", "match")
    assert (row["cost_micros"], row["cost_basis"]) == (49000, LIST_PRICE)
    assert row["subject"] == "acme.com"
    p = r["provenance"]
    assert p["ledger_entry"] == row["id"] and p["response_digest"] == row["response_digest"]
    assert p["request_digest"] == row["request_digest"] and len(p["request_digest"]) == 64


def test_ledger_stores_no_person_and_no_address_in_clear():
    m, ledger = _m(prices={"hunter.find_email": 1, "hunter.verify_email": 1})
    m.find_email(domain="acme.com", first_name="Dana", last_name="Rivera")
    m.verify_email("dana@acme.com")
    blob = repr(ledger.entries())
    assert "Dana" not in blob and "Rivera" not in blob and "dana@" not in blob
    assert ledger.entries()[1]["subject"] == "acme.com"


def test_unpriced_call_is_booked_unknown_never_zero():
    m, ledger = _m(prices={})
    r = m.find_email(domain="acme.com", first_name="a", last_name="b")
    row = ledger.entries()[0]
    assert row["cost_micros"] is None and row["cost_basis"] == UNKNOWN
    assert r["provenance"]["cost_micros"] is None
    assert ledger.summary("t1") == {"tenant": "t1", "known_spend_micros": 0, "calls": 1,
                                    "unknown_cost_calls": 1, "refused_calls": 0}


def test_budget_refuses_before_the_provider_is_contacted():
    fake = Fake()
    m, ledger = _m(fake, prices={"hunter.find_email": 60}, budget_micros=100)
    m.find_email(domain="acme.com", first_name="a", last_name="b")
    with pytest.raises(BudgetExceeded):
        m.find_email(domain="acme.com", first_name="c", last_name="d")
    assert fake.calls == ["find_email"]                      # the second call never left the process
    refused = ledger.entries()[1]
    assert (refused["outcome"], refused["cost_basis"], refused["cost_micros"]) == ("refused_budget", NOT_CALLED, 0)
    assert ledger.summary("t1")["known_spend_micros"] == 60


def test_budget_with_an_unknown_price_fails_closed():
    fake = Fake()
    m, _ = _m(fake, prices={}, budget_micros=10_000_000)
    with pytest.raises(BudgetExceeded, match="no configured price"):
        m.find_email(domain="acme.com", first_name="a", last_name="b")
    assert fake.calls == []


def test_provider_error_is_booked_as_if_billed():
    m, ledger = _m(Fake(fail=True), prices={"hunter.find_email": 49000})
    with pytest.raises(E.EnrichmentError):
        m.find_email(domain="acme.com", first_name="a", last_name="b")
    row = ledger.entries()[0]
    assert (row["outcome"], row["cost_micros"], row["cost_basis"]) == ("error", 49000, LIST_PRICE_ON_ERROR)
    assert "api_key" not in row["detail"]


def test_no_match_is_metered_and_not_a_match():
    m, ledger = _m(Fake(email=None), prices={"hunter.find_email": 5})
    assert m.find_email(domain="acme.com", first_name="a", last_name="b")["email"] is None
    assert ledger.entries()[0]["outcome"] == "no_match"


def test_ledger_is_append_only():
    m, ledger = _m(prices={"hunter.find_email": 5})
    m.find_email(domain="acme.com", first_name="a", last_name="b")
    for sql in ("UPDATE enrichment_calls SET cost_micros = 0", "DELETE FROM enrichment_calls"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            ledger._db.execute(sql)
    assert ledger.entries()[0]["cost_micros"] == 5


def test_capability_probing_is_preserved():
    m, _ = _m()
    assert hasattr(m, "find_email") and not hasattr(m, "search_people") and not hasattr(m, "unlock_person")


def test_resolve_verified_email_carries_both_calls_provenance():
    m, ledger = _m(prices={"hunter.find_email": 49000, "hunter.verify_email": 10000})
    r = E.resolve_verified_email(domain="acme.com", first_name="Dana", last_name="Rivera", provider=m)
    assert r["email"] == "dana@acme.com" and r["verified"] is True
    assert [p["operation"] for p in r["provenance"]] == ["find_email", "verify_email"]
    assert ledger.summary("t1")["known_spend_micros"] == 59000


def test_resolve_verified_email_over_budget_degrades_to_no_email():
    m, _ = _m(prices={"hunter.find_email": 49000}, budget_micros=1)
    r = E.resolve_verified_email(domain="acme.com", first_name="Dana", last_name="Rivera", provider=m)
    assert r["email"] is None and r["verified"] is False and "budget" in r["reason"]


def test_unmetered_result_has_empty_provenance():
    r = E.resolve_verified_email(domain="acme.com", first_name="Dana", last_name="Rivera", provider=Fake())
    assert r["provenance"] == []


def test_get_provider_is_metered_when_a_ledger_is_deployed(monkeypatch, tmp_path):
    monkeypatch.setenv("HUNTER_API_KEY", "k")
    monkeypatch.setenv("REDEVOPS_ENRICHMENT_PROVIDER", "hunter")
    monkeypatch.delenv("ENRICHMENT_LEDGER_PATH", raising=False)
    assert isinstance(E.get_provider(), E.HunterProvider)
    monkeypatch.setenv("ENRICHMENT_LEDGER_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.delenv("ENRICHMENT_TENANT", raising=False)
    with pytest.raises(E.EnrichmentError, match="ENRICHMENT_TENANT"):
        E.get_provider()                                     # half-configured: no silent unmetered spend
    monkeypatch.setenv("ENRICHMENT_TENANT", "t1")
    monkeypatch.setenv("ENRICHMENT_BUDGET_MICROS", "500")
    p = E.get_provider()
    assert isinstance(p, MeteredProvider) and p.name == "hunter" and p._budget == 500


def test_prices_from_env_rejects_non_integer_prices(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_PRICE_MICROS", '{"hunter.find_email": 0.049}')
    with pytest.raises(E.EnrichmentError):
        prices_from_env()
    monkeypatch.setenv("ENRICHMENT_PRICE_MICROS", '{"hunter.find_email": 49000}')
    assert prices_from_env() == {"hunter.find_email": 49000}
