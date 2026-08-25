"""HubSpot Sales Sequences cold-send adapter — offline (HTTP mocked). Contact upsert + sequence enrollment."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentic_os.world import connectors as C  # noqa: E402
from agentic_os.world.connectors import HubSpotSequenceSender  # noqa: E402
from agentic_os.world.outreach import OutreachContext, send_outreach  # noqa: E402


def _grounded(email):
    return OutreachContext(company="Acme", first_name="Jordan", role="Head of AI", verified_email=email,
                           observed_activity="hiring around agent sandboxing",
                           specific_evidence_sentence="Your careers page lists an agent-infrastructure role "
                                                      "covering sandboxing and permission scoping.",
                           runtime_problem="execution policy is re-implemented per system",
                           evidence_about_company="the hiring", specific_problem_or_workflow="coding agents")


def test_enrollment_succeeds_when_scope_present(monkeypatch):
    calls = {"upsert": 0, "enroll": 0}

    def fake_http(method, url, *, headers, data=None, timeout=12.0):
        if url.endswith("/contacts/search"):
            return {"results": []}
        if url.endswith("/contacts") and method == "POST":
            calls["upsert"] += 1; return {"id": "contact-1"}
        if "sequences?limit=1" in url:
            return {"results": []}                       # scope present
        if url.endswith("/sequences/enrollments"):
            calls["enroll"] += 1; return {"id": "enroll-1"}
        return {}

    monkeypatch.setattr(C, "_http", fake_http)
    s = HubSpotSequenceSender(token="tok", sequence_id="seq-9", sender_id="owner-3")
    r = s.send(to="jordan@acme.com", subject="hi", body="grounded evidence")
    assert r["error_code"] == 0 and r["message_id"] == "enroll-1" and r["contact_id"] == "contact-1"
    assert calls["upsert"] == 1 and calls["enroll"] == 1


def test_degrades_clearly_without_sequences_scope(monkeypatch):
    def fake_http(method, url, *, headers, data=None, timeout=12.0):
        if url.endswith("/contacts/search"):
            return {"results": [{"id": "contact-1"}]}
        if "sequences?limit=1" in url:
            raise C.ConnectorError("GET .../sequences -> HTTP 403")   # scope/tier missing
        if method == "PATCH":
            return {"id": "contact-1"}
        return {}

    monkeypatch.setattr(C, "_http", fake_http)
    s = HubSpotSequenceSender(token="tok", sequence_id="seq-9", sender_id="owner-3")
    r = s.send(to="jordan@acme.com", subject="hi", body="grounded")
    # contact still upserted; enrollment reports the exact missing requirement, does not pretend to send
    assert r["error_code"] == 1 and "sequences scope" in r["reason"] and r["contact_id"] == "contact-1"


def test_is_a_drop_in_sender_for_send_outreach(monkeypatch):
    def fake_http(method, url, *, headers, data=None, timeout=12.0):
        if url.endswith("/contacts/search"):
            return {"results": []}
        if url.endswith("/contacts") and method == "POST":
            return {"id": "c1"}
        if "sequences?limit=1" in url:
            return {"results": []}
        if url.endswith("/sequences/enrollments"):
            return {"id": "e1"}
        return {}

    monkeypatch.setattr(C, "_http", fake_http)
    sender = HubSpotSequenceSender(token="tok", sequence_id="s", sender_id="o")
    r = send_outreach(_grounded("jordan@acme.com"), sender, cap_remaining=5, auto_send=True)
    assert r["decision"] == "SEND" and r["sent"] is True    # HubSpot sender interchangeable with Postmark


def test_apollo_sender_degrades_without_paid_setup(monkeypatch):
    from agentic_os.world.connectors import ApolloSender
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    monkeypatch.delenv("APOLLO_SEQUENCE_ID", raising=False)
    monkeypatch.delenv("APOLLO_SENDER_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(C, "_http", lambda *a, **k: {"is_logged_in": True})
    r = ApolloSender().send(to="a@b.com", subject="s", body="b")
    assert r["error_code"] == 1 and "APOLLO_SEQUENCE_ID" in r["reason"]   # clear reason, no silent send


def test_apollo_sender_sends_when_configured(monkeypatch):
    from agentic_os.world.connectors import ApolloSender
    def fake_http(method, url, *, headers, data=None, timeout=12.0):
        if url.endswith("/auth/health"): return {"is_logged_in": True}
        if url.endswith("/contacts"): return {"contact": {"id": "ac-1"}}
        if "add_contact_ids" in url: return {"contacts": [{"id": "ac-1"}]}
        return {}
    monkeypatch.setattr(C, "_http", fake_http)
    r = ApolloSender(key="k", sequence_id="seq-1", sender_account_id="acct-1").send(to="a@b.com", subject="s", body="b")
    assert r["error_code"] == 0 and r["contact_id"] == "ac-1"
