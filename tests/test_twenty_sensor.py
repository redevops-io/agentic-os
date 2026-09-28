"""Twenty CRM sensor — mappers + STALLED scan (unit, stub) + a live read-only smoke."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentic_os.integrations import twenty as tw

_NOW = int(datetime(2026, 9, 28, tzinfo=timezone.utc).timestamp() * 1000)


def _iso(days_ago: int) -> str:
    return (datetime(2026, 9, 28, tzinfo=timezone.utc) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


class _StubTwenty:
    def opportunities(self, *, limit=60):
        return [
            {"id": "o1", "name": "Old deal", "stage": "PROPOSAL",
             "amount": {"amountMicros": 1_000_000_000_000, "currencyCode": "USD"}, "updatedAt": _iso(60)},
            {"id": "o2", "name": "Fresh deal", "stage": "MEETING",
             "amount": {"amountMicros": 500_000_000_000, "currencyCode": "USD"}, "updatedAt": _iso(2)},
            {"id": "o3", "name": "Won deal", "stage": "CUSTOMER",
             "amount": {"amountMicros": 9_000_000_000_000, "currencyCode": "USD"}, "updatedAt": _iso(90)},
        ]

    def companies(self, *, limit=60):
        return [{"id": "c1", "name": "Acme", "domainName": {"primaryLinkUrl": "acme.com"}}]


def test_to_opportunity_maps_amount_and_stage() -> None:
    o, last = tw.to_opportunity({"id": "o1", "name": "X", "stage": "PROPOSAL",
                                 "amount": {"amountMicros": 2_500_000_000_000, "currencyCode": "USD"},
                                 "updatedAt": _iso(1)})
    assert o.amount_cents == 250_000_000 and o.currency == "USD" and o.stage == "proposal"   # $2.5M
    assert last > 0
    won, _ = tw.to_opportunity({"id": "o3", "stage": "CUSTOMER", "updatedAt": _iso(1)})
    assert won.stage == "closed_won"     # terminal Twenty stage normalized


def test_to_account_pulls_domain() -> None:
    a = tw.to_account({"id": "c1", "name": "Acme", "domainName": {"primaryLinkUrl": "acme.com"}})
    assert a.name == "Acme" and a.domain == "acme.com" and a.prov.provider_ref == "c1"


def test_scan_stalled_flags_only_the_old_active_deal() -> None:
    leaks = tw.scan_stalled(_StubTwenty(), now_ms=_NOW, stale_days=14)
    assert len(leaks) == 1                       # o1 only (o2 fresh, o3 terminal)
    assert leaks[0].subject == "Old deal" and leaks[0].amount_cents == 100_000_000


def test_live_twenty_scan() -> None:
    # read-only; self-skips unless TWENTY_BASE_URL + TWENTY_API_KEY are set + reachable
    import time
    client = tw.twenty_from_env()
    if client is None or not client.opportunities(limit=1):
        __import__("pytest").skip("no reachable/authorized Twenty (TWENTY_BASE_URL / TWENTY_API_KEY)")
    leaks = tw.scan_stalled(client, now_ms=int(time.time() * 1000))
    assert isinstance(leaks, list)               # real pipeline → a (possibly empty) list of leakage signals
