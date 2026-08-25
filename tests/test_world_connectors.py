"""GTM connectors — HubSpot / Salesforce / Postmark. Offline: HTTP is monkeypatched; no network, no secrets.

Live verification (Salesforce ping ✓, Postmark real send to self ✓, HubSpot via HUBSPOT_SERVICE_KEY ✓) was
done manually against the real APIs on evo-x2; these tests lock the request shapes + graceful degradation.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentic_os.world import connectors as C  # noqa: E402


def test_connectors_degrade_gracefully_without_creds(monkeypatch):
    for v in ("HUBSPOT_SERVICE_KEY", "HUBSPOT_API_KEY", "SALESFORCE_CONSUMER_KEY",
              "SALESFORCE_CONSUMER_SECRET", "SALESFORCE_INSTANCE_URL"):
        monkeypatch.delenv(v, raising=False)
    assert C.HubSpotConnector().ping()["connected"] is False        # no token → not connected, no crash
    assert C.SalesforceConnector().ping()["connected"] is False     # missing client creds → not connected


def test_postmark_send_request_shape(monkeypatch):
    calls = {}

    def fake_http(method, url, *, headers, data=None, timeout=12.0):
        calls["method"], calls["url"], calls["headers"] = method, url, headers
        calls["body"] = __import__("json").loads(data.decode()) if data else None
        return {"MessageID": "mid-1", "To": calls["body"]["To"], "ErrorCode": 0}

    monkeypatch.setattr(C, "_http", fake_http)
    pm = C.PostmarkConnector("server-token-xyz", from_addr="hello@redevops.io")
    r = pm.send(to="prospect@example.com", subject="hi", body="grounded evidence", tag="gtm")
    assert r["error_code"] == 0 and r["message_id"] == "mid-1"
    # server token goes in the Postmark header, never the URL; correct endpoint + stream
    assert calls["headers"]["X-Postmark-Server-Token"] == "server-token-xyz"
    assert calls["url"] == "https://api.postmarkapp.com/email"
    assert calls["body"]["MessageStream"] == "outbound" and calls["body"]["From"] == "hello@redevops.io"


def test_hubspot_upsert_creates_then_patches(monkeypatch):
    state = {"exists": False}

    def fake_http(method, url, *, headers, data=None, timeout=12.0):
        assert headers["Authorization"].startswith("Bearer ")
        if url.endswith("/search"):
            return {"results": ([{"id": "co-1"}] if state["exists"] else [])}
        if method == "POST":
            return {"id": "co-1"}
        if method == "PATCH":
            return {"id": "co-1"}
        return {}

    monkeypatch.setattr(C, "_http", fake_http)
    hs = C.HubSpotConnector(token="tok")
    a = hs.upsert_company(name="Acme", domain="acme.com")
    assert a["created"] is True and a["id"] == "co-1"
    state["exists"] = True
    b = hs.upsert_company(name="Acme", domain="acme.com")
    assert b["created"] is False and b["id"] == "co-1"       # dedup → patch, not a duplicate


def test_gtm_live_reconcile_reads_hubspot(monkeypatch):
    # with the live flag + a real (mocked) HubSpot read, reconcile reports dedup without any write
    from agentic_os.world.gtm import FindCompaniesRebuildingTheRuntime
    monkeypatch.setenv("REDEVOPS_LIVE_CONNECTORS", "1")
    monkeypatch.setattr(C.HubSpotConnector, "lookup_company", lambda self, domain: {"id": "co-9"})
    out = FindCompaniesRebuildingTheRuntime()._reconcile({"org_id": "acme", "domain": "acme.com"}, {})
    assert out["source"] == "hubspot-live-read" and out["hubspot_id"] == "co-9" and out["duplicate"] is True


def test_gtm_simulated_reconcile_is_default(monkeypatch):
    from agentic_os.world.gtm import FindCompaniesRebuildingTheRuntime
    monkeypatch.delenv("REDEVOPS_LIVE_CONNECTORS", raising=False)
    out = FindCompaniesRebuildingTheRuntime()._reconcile({"org_id": "acme"}, {})
    assert out["source"] == "simulated" and out["duplicate"] is False
