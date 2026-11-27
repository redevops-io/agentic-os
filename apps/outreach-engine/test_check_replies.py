"""outreach-engine's reply detection (`core.check_replies`), reading the sender's own Gmail inbox.

Every test here fakes the reader -- never the real network -- and the autouse fixture forces
GMAIL_SMTP_USER/APP_PASSWORD off regardless of this machine's real ~/.zshrc exports, exactly like
test_send_wiring.py. GmailIMAPConnector itself is covered by tests/test_world_connectors.py.
"""
from __future__ import annotations

import importlib

import pytest

core = importlib.import_module("outreach-engine.core")


class _FakeReader:
    def __init__(self, by_address=None, error_for=None):
        self._by_address = by_address or {}
        self._error_for = error_for or set()
        self.calls = []

    def find_replies(self, from_addr, *, limit=5):
        self.calls.append(from_addr)
        if from_addr in self._error_for:
            return {"ok": False, "replies": [], "error": "IMAP4.error: boom"}
        return {"ok": True, "replies": self._by_address.get(from_addr, [])}


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    core._STATE.update(accounts=[], approved=set(), candidates=[])
    monkeypatch.setattr(core, "GMAIL_SMTP_USER", "")
    monkeypatch.setattr(core, "GMAIL_SMTP_APP_PASSWORD", "")
    yield


def test_check_replies_is_honest_with_no_reader_configured():
    res = core.check_replies({})
    assert res == {"ok": False, "action": "check_replies", "reason": "no reader (IMAP) configured", "results": []}


def test_check_replies_unknown_account(monkeypatch):
    monkeypatch.setattr(core, "_reader", lambda: _FakeReader())
    res = core.check_replies({"account": "nope"})
    assert res["ok"] is False and "refresh the pipeline" in res["reason"]


def test_check_replies_finds_a_real_reply_for_one_account(monkeypatch):
    reply = {"from": "Tasha <tasha@nutrients.tech>", "subject": "Re: pitch", "date": "2026-11-14T12:00:00"}
    fake = _FakeReader(by_address={"tasha@nutrients.tech": [reply]})
    monkeypatch.setattr(core, "_reader", lambda: fake)
    core._STATE["accounts"] = [{"account": "Acme", "contact_email": "tasha@nutrients.tech"}]
    core._STATE["approved"] = {"Acme"}
    res = core.check_replies({"account": "Acme"})
    assert res["ok"] is True
    entry = res["results"][0]
    assert entry["replied"] is True and entry["reply"] == reply
    row = core._STATE["accounts"][0]
    assert row["replied"] is True and row["reply"] == reply
    assert fake.calls == ["tasha@nutrients.tech"]


def test_check_replies_checks_every_approved_account_when_none_is_named(monkeypatch):
    fake = _FakeReader(by_address={"a@x.com": [{"from": "a@x.com", "subject": "re", "date": None}]})
    monkeypatch.setattr(core, "_reader", lambda: fake)
    core._STATE["accounts"] = [
        {"account": "Approved A", "contact_email": "a@x.com"},
        {"account": "Approved B", "contact_email": "b@x.com"},
        {"account": "Not Approved", "contact_email": "c@x.com"},
    ]
    core._STATE["approved"] = {"Approved A", "Approved B"}
    res = core.check_replies({})
    by_acct = {r["account"]: r for r in res["results"]}
    assert set(by_acct) == {"Approved A", "Approved B"}       # unapproved accounts are never checked
    assert by_acct["Approved A"]["replied"] is True
    assert by_acct["Approved B"]["replied"] is False
    assert sorted(fake.calls) == ["a@x.com", "b@x.com"]


def test_check_replies_reports_no_contact_email_without_crashing(monkeypatch):
    monkeypatch.setattr(core, "_reader", lambda: _FakeReader())
    core._STATE["accounts"] = [{"account": "No Email Co", "contact_email": None}]
    core._STATE["approved"] = {"No Email Co"}
    res = core.check_replies({})
    assert res["results"][0]["replied"] is False
    assert "enrich" in res["results"][0]["reason"]


def test_check_replies_surfaces_an_imap_failure_instead_of_claiming_no_reply(monkeypatch):
    fake = _FakeReader(error_for={"bad@x.com"})
    monkeypatch.setattr(core, "_reader", lambda: fake)
    core._STATE["accounts"] = [{"account": "Flaky", "contact_email": "bad@x.com"}]
    core._STATE["approved"] = {"Flaky"}
    res = core.check_replies({})
    entry = res["results"][0]
    assert entry["replied"] is False and "IMAP4.error" in entry["reason"]
    assert "replied" not in core._STATE["accounts"][0] or core._STATE["accounts"][0].get("replied") is not True


def test_reader_is_none_without_both_credentials(monkeypatch):
    monkeypatch.setattr(core, "GMAIL_SMTP_USER", "alex@joinredevops.com")
    monkeypatch.setattr(core, "GMAIL_SMTP_APP_PASSWORD", "")
    assert core._reader() is None
