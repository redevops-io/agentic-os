"""outreach-engine's real sender (Gmail SMTP app-password) wiring for `send_all`.

`send_all` must: (1) stay the exact honest no-op it already was when no sender is configured; (2) once a
sender IS configured, send only to approved accounts with a VERIFIED contact_email (from `enrich`), never a
fabricated or unverified address; (3) never touch the real network — every test here fakes the connector or
monkeypatches `core.GMAIL_SMTP_USER`/`GMAIL_SMTP_APP_PASSWORD` off, regardless of this machine's real
~/.zshrc exports. `GmailSMTPConnector` itself is covered by tests/test_world_connectors.py.
"""
from __future__ import annotations

import importlib

import pytest

core = importlib.import_module("outreach-engine.core")


class _FakeSender:
    def __init__(self):
        self.calls = []

    def send(self, *, to, subject, body, tag="gtm", html=None):
        self.calls.append({"to": to, "subject": subject, "body": body, "tag": tag, "html": html})
        if to == "bounces@example.com":
            return {"message_id": None, "to": to, "error_code": 1, "error": "RuntimeError: mailbox full"}
        return {"message_id": f"mid-{len(self.calls)}", "to": to, "error_code": 0}


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    core._STATE.update(accounts=[], approved=set(), candidates=[])
    monkeypatch.setattr(core, "GMAIL_SMTP_USER", "")
    monkeypatch.setattr(core, "GMAIL_SMTP_APP_PASSWORD", "")
    yield


def _approved_row(account="Acme", **extra):
    return {"account": account, "draft": {"subject": "hi " + account, "body": "pitch body"}, **extra}


def test_send_all_is_still_the_honest_no_op_with_no_sender_configured():
    core._STATE["approved"] = {"Acme"}
    res = core.send_all({"_approval": {"approved": True}})
    assert res == {"ok": False, "action": "send_all", "status": "not_configured", "executed": False,
                   "sequences_sent": False, "dispatched": 0, "staged": ["Acme"], "sender_configured": False,
                   "reason": "no sender (SMTP/provider) is wired into this deployment",
                   "note": "no live sender configured — approved sequences are staged, not sent. "
                           "Wire a sender (SMTP/provider) to actually dispatch."}


def test_sender_is_none_without_both_credentials(monkeypatch):
    monkeypatch.setattr(core, "GMAIL_SMTP_USER", "alex@joinredevops.com")
    monkeypatch.setattr(core, "GMAIL_SMTP_APP_PASSWORD", "")
    assert core._sender() is None
    monkeypatch.setattr(core, "GMAIL_SMTP_USER", "")
    monkeypatch.setattr(core, "GMAIL_SMTP_APP_PASSWORD", "app-pw")
    assert core._sender() is None


def test_send_all_sends_only_to_verified_accounts_once_a_sender_is_wired(monkeypatch):
    fake = _FakeSender()
    monkeypatch.setattr(core, "_sender", lambda: fake)
    core._STATE["accounts"] = [
        _approved_row("Verified Co", contact_email="rob@kaizenrw.com", email_verified=True),
        _approved_row("Unverified Co", contact_email="maybe@acme.com", email_verified=False),
        _approved_row("No Email Co", contact_email=None, email_verified=False),
    ]
    core._STATE["approved"] = {"Verified Co", "Unverified Co", "No Email Co"}
    res = core.send_all({"_approval": {"approved": True}})
    assert res["ok"] is True and res["sender_configured"] is True
    assert res["dispatched"] == 1 and res["sequences_sent"] is True and res["status"] == "dispatched"
    by_acct = {r["account"]: r for r in res["results"]}
    assert by_acct["Verified Co"]["sent"] is True and by_acct["Verified Co"]["to"] == "rob@kaizenrw.com"
    assert by_acct["Unverified Co"]["sent"] is False and "verified" in by_acct["Unverified Co"]["reason"]
    assert by_acct["No Email Co"]["sent"] is False and "verified" in by_acct["No Email Co"]["reason"]
    # exactly one real send attempt was made, and only to the verified recipient
    assert [c["to"] for c in fake.calls] == ["rob@kaizenrw.com"]
    assert fake.calls[0]["subject"] == "hi Verified Co" and fake.calls[0]["tag"] == "outreach-engine"
    assert "<table" in fake.calls[0]["html"]   # the evidence visualization is a real, generated fallback


def test_send_all_reports_a_provider_failure_without_crashing(monkeypatch):
    fake = _FakeSender()
    monkeypatch.setattr(core, "_sender", lambda: fake)
    core._STATE["accounts"] = [_approved_row("Bouncer", contact_email="bounces@example.com", email_verified=True)]
    core._STATE["approved"] = {"Bouncer"}
    res = core.send_all({"_approval": {"approved": True}})
    assert res["dispatched"] == 0 and res["sequences_sent"] is False and res["status"] == "no_eligible_recipients"
    r = res["results"][0]
    assert r["sent"] is False and r["reason"] == "RuntimeError: mailbox full"


def test_send_all_with_no_draft_falls_back_to_teardown(monkeypatch):
    fake = _FakeSender()
    monkeypatch.setattr(core, "_sender", lambda: fake)
    core._STATE["accounts"] = [{"account": "Whitfield Family Trust", "signal": "cold",
                                "contact_email": "trustee@example.com", "email_verified": True}]
    core._STATE["approved"] = {"Whitfield Family Trust"}
    res = core.send_all({"_approval": {"approved": True}})
    assert res["dispatched"] == 1
    assert "Whitfield Family Trust" in fake.calls[0]["subject"]


def test_send_all_still_stages_without_approval_even_with_a_sender_configured(monkeypatch):
    monkeypatch.setattr(core, "GMAIL_SMTP_USER", "alex@joinredevops.com")
    monkeypatch.setattr(core, "GMAIL_SMTP_APP_PASSWORD", "app-pw")
    core._STATE["approved"] = {"Acme"}
    res = core.send_all({})
    assert res["status"] == "pending_approval" and res["executed"] is False and res["sequences_sent"] is False
