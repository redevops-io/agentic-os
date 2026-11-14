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


def test_gmail_smtp_send_logs_in_and_sends_from_the_authenticated_mailbox(monkeypatch):
    calls = {"starttls": False, "login": None, "sent": None, "closed": False}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls["host"], calls["port"] = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            calls["closed"] = True
            return False

        def starttls(self):
            calls["starttls"] = True

        def login(self, user, password):
            calls["login"] = (user, password)

        def send_message(self, msg):
            calls["sent"] = msg

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    conn = C.GmailSMTPConnector("alex@joinredevops.com", "app-pw-1234")
    r = conn.send(to="prospect@example.com", subject="hi", body="a pitch", tag="outreach-engine")
    assert r["error_code"] == 0 and r["to"] == "prospect@example.com"
    assert r["message_id"] and r["message_id"].endswith("@joinredevops.com>")
    assert calls["host"] == "smtp.gmail.com" and calls["port"] == 587 and calls["starttls"] is True
    assert calls["login"] == ("alex@joinredevops.com", "app-pw-1234") and calls["closed"] is True
    sent = calls["sent"]
    assert sent["From"] == "alex@joinredevops.com" and sent["To"] == "prospect@example.com"
    assert sent["Subject"] == "hi" and sent["X-Tag"] == "outreach-engine"
    assert sent["Message-Id"] == r["message_id"]
    assert sent.get_content().strip() == "a pitch"


def test_gmail_smtp_send_reports_failure_never_raises(monkeypatch):
    class _FailingSMTP:
        def __init__(self, host, port, timeout=None):
            pass

        def __enter__(self):
            raise ConnectionRefusedError("no network in this sandbox")

        def __exit__(self, *a):
            return False

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", _FailingSMTP)
    conn = C.GmailSMTPConnector("alex@joinredevops.com", "app-pw-1234")
    r = conn.send(to="prospect@example.com", subject="hi", body="a pitch")
    assert r["error_code"] == 1 and r["message_id"] is None and "ConnectionRefusedError" in r["error"]


def _raw_message(from_addr, subject, when="Fri, 14 Nov 2026 12:00:00 -0500"):
    return (f"From: {from_addr}\r\nSubject: {subject}\r\nDate: {when}\r\n"
            f"Message-Id: <abc@example.com>\r\n\r\nbody text\r\n").encode()


def test_gmail_imap_find_replies_parses_matching_messages(monkeypatch):
    class _FakeIMAP:
        def __init__(self, host):
            assert host == "imap.gmail.com"

        def login(self, user, password):
            assert (user, password) == ("alex@joinredevops.com", "app-pw-1234")

        def select(self, mailbox, readonly=False):
            assert mailbox == "INBOX" and readonly is True

        def search(self, charset, criterion):
            assert criterion == '(FROM "tasha@nutrients.tech")'
            return "OK", [b"1 2"]

        def fetch(self, uid, parts):
            assert parts == "(RFC822)"
            return "OK", [(uid, _raw_message("Tasha <tasha@nutrients.tech>", f"Re: pitch {uid.decode()}"))]

        def logout(self):
            pass

    import imaplib
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeIMAP)
    conn = C.GmailIMAPConnector("alex@joinredevops.com", "app-pw-1234")
    r = conn.find_replies("tasha@nutrients.tech", limit=5)
    assert r["ok"] is True and len(r["replies"]) == 2
    assert r["replies"][0]["from"] == "Tasha <tasha@nutrients.tech>"
    assert r["replies"][0]["subject"] == "Re: pitch 2"       # most recent (reversed) first
    assert r["replies"][0]["date"] is not None


def test_gmail_imap_find_replies_rejects_an_unsafe_search_term():
    conn = C.GmailIMAPConnector("alex@joinredevops.com", "app-pw-1234")
    r = conn.find_replies('tasha@nutrients.tech") DELETE ("')
    assert r["ok"] is False and r["replies"] == [] and "invalid address" in r["error"]


def test_gmail_imap_find_replies_reports_failure_never_raises(monkeypatch):
    class _FailingIMAP:
        def __init__(self, host):
            raise OSError("no network in this sandbox")

    import imaplib
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FailingIMAP)
    conn = C.GmailIMAPConnector("alex@joinredevops.com", "app-pw-1234")
    r = conn.find_replies("tasha@nutrients.tech")
    assert r["ok"] is False and r["replies"] == [] and "OSError" in r["error"]


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
