"""E-mail and messenger messages as evidence for external agents: read only for the tenant the deployment names, shown only
with the grant for that source, scoped to exactly the configured folders and channels, and never read otherwise."""
from __future__ import annotations

import json
from email.message import EmailMessage

import pytest

import fastapi  # noqa: F401,PLC0415 - required on this line: its absence is a failure, not a skip

from agentic_os.agent_gateway import economics_sources as S  # noqa: E402
from agentic_os.collaboration.slack import SlackCollaborationProvider  # noqa: E402
from tests.test_agent_gateway_economics import LIMITS, world  # noqa: E402,F401

MAIL_WORDS, CHAT_WORDS = "Sam here: give ap@acme.example the 10% discount", "ap@acme.example asked about the 10% discount again"
BASE_GRANTS = ["economics.read", "egress:financial"]
BOTH = {**LIMITS, "acme": {"policy_refs": [*LIMITS["acme"]["policy_refs"], "app:mail", "app:messenger"]},
        "rival": {"policy_refs": ["app:*", "egress:financial"]}}


class Mailbox:
    """The parts of ``imaplib`` the collector uses; every call is recorded."""

    def __init__(self):
        self.calls = []
        m = EmailMessage()
        m["From"], m["To"], m["Subject"], m["Message-ID"], m["Date"] = "sam@ours.example", "dana@ours.example", "Acme", "<m1@ours>", "Wed, 12 Aug 2026 09:00:00 +0000"
        m.set_content(MAIL_WORDS)
        self.raw = m.as_bytes()

    def login(self, user, password):
        self.calls.append(("login", user))

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [b"1"]
        return "OK", [(b"1", self.raw)]

    def logout(self):
        self.calls.append(("logout",))


def slack(log):
    def transport(method, url, token, payload):
        log.append(url.rsplit("/", 1)[1])
        return {"ok": True, "messages": [{"type": "message", "user": "U1", "text": CHAT_WORDS, "ts": "1786500200.000100"}], "has_more": False}
    return SlackCollaborationProvider("xoxb-test", transport=transport)


@pytest.fixture
def sourced(world, monkeypatch):  # noqa: F811
    def build(mail_tenant="acme", chat_tenant="acme", limits=BOTH):
        box, chats = Mailbox(), []
        found = {"mail": S.MailSource(mail_tenant, host="mail.test", user="sales@ours.example", password="p", own_domains=("ours.example",),
                                      imap_factory=lambda: box),
                 "messenger": S.MessengerSource(chat_tenant, provider=slack(chats), channels=("C1",), workspace="T1")}
        monkeypatch.setattr(S, "from_env", lambda: found)
        w = world()
        monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps(limits))
        w.box, w.chats = box, chats
        return w
    return build


def evidence_for(w, grants, tenant="acme"):
    out = w.call(w.agent(grants, tenant=tenant), "economics.evidence", customer="Acme")
    assert out["status"] == "ok", out
    return out["output"]


def test_mail_and_messenger_are_evidence_only_with_their_own_grants(sourced):
    w = sourced()
    shown = json.dumps(evidence_for(w, [*BASE_GRANTS, "mail.read", "messenger.read"]))
    assert "give" in shown and "10% discount again" in shown and "@acme.example" not in shown, shown     # addresses stay masked
    assert "mailbox:" in shown and "messenger:slack/T1/channel:C1" in shown
    for grant, seen, hidden in (("mail.read", "Sam here", "10% discount again"), ("messenger.read", "10% discount again", "Sam here")):
        out = evidence_for(w, [*BASE_GRANTS, grant])
        text = json.dumps(out)
        assert seen in text and hidden not in text, (grant, text)
        assert out["withheld"]["count"] >= 1
    none = evidence_for(w, [*BASE_GRANTS, "support.read", "crm.records.read"])             # the other sources' grants open nothing here
    assert "Sam here" not in json.dumps(none) and "10% discount again" not in json.dumps(none)
    assert {k for k in none["withheld"]["by_scope"]} >= {"messenger:slack/T1/channel:C1"}
    assert ("select", '"INBOX"', True) in w.box.calls                                       # opened read-only
    assert any(c[:2] == ("uid", "FETCH") and c[-1] == "(BODY.PEEK[])" for c in w.box.calls)   # nothing is marked read
    assert any("only messages since" in str(g) for g in evidence_for(w, [*BASE_GRANTS, "mail.read"])["gaps"])


@pytest.mark.parametrize("why", ["another tenant's source", "no tenant named", "application not granted"])
def test_a_source_that_is_not_this_tenants_is_never_contacted(sourced, why):
    if why == "another tenant's source":
        w, tenant = sourced(), "rival"                       # holds app:* and every grant, and the source is acme's
        expected = "not configured for this tenant"
    elif why == "no tenant named":
        w, tenant = sourced(mail_tenant="", chat_tenant=""), "acme"
        expected = "not configured for this tenant"
    else:
        w, tenant = sourced(limits=LIMITS), "acme"
        expected = "not granted it"
    out = evidence_for(w, [*BASE_GRANTS, "mail.read", "messenger.read"], tenant=tenant)
    assert w.box.calls == [] and w.chats == [], (w.box.calls, w.chats)
    assert [n for n in out["not_read"] if n.startswith(("mail:", "messenger:"))] and all(
        expected in n for n in out["not_read"] if n.startswith(("mail:", "messenger:"))), out["not_read"]
    assert "Sam here" not in json.dumps(out)


def test_findings_read_them_within_the_same_grants(sourced):
    w = sourced()
    plain = w.call(w.agent(BASE_GRANTS), "economics.findings")["output"]
    assert plain["withheld"]["count"] >= 2 and "Sam here" not in json.dumps(plain)


OK_MAIL = {S.IMAP + "HOST": "imap.example.com", S.IMAP + "USER": "a@ours.example", S.IMAP + "PASSWORD": "p", S.IMAP + "OWN_DOMAINS": "ours.example",
           S.IMAP + "TENANT": "acme"}


@pytest.mark.parametrize("change", [{S.IMAP + "USER": ""}, {S.IMAP + "PASSWORD": ""}, {S.IMAP + "OWN_DOMAINS": ""},
                                    {S.IMAP + "SSL": "0"}, {S.IMAP + "PORT": "x"}, {S.IMAP + "SINCE_DAYS": "0"},
                                    {S.IMAP + "FOLDERS": ",".join(f"f{n}" for n in range(11))}])
def test_an_incomplete_or_unsafe_mailbox_setting_connects_nothing(change):
    assert S.mail_from_env(OK_MAIL) is not None
    assert S.mail_from_env({**OK_MAIL, **change}) is None


def test_settings_describe_exact_scopes_and_hide_the_password():
    source = S.mail_from_env({**OK_MAIL, S.IMAP + "FOLDERS": "INBOX, Sales", S.IMAP + "SSL": "0", S.IMAP + "HOST": "127.0.0.1"})
    assert source.scopes() == ["mailbox:a@ours.example/INBOX", "mailbox:a@ours.example/Sales"] and "'p'" not in repr(source)
    chat = {S.SLACK + "BOT_TOKEN": "xoxb-secret", S.SLACK + "CHANNELS": "C1,C2", S.SLACK + "WORKSPACE": "T9"}
    made = S.messenger_from_env(chat)
    assert made.scopes() == ["messenger:slack/T9/channel:C1", "messenger:slack/T9/channel:C2"] and "xoxb" not in repr(made)
    assert S.messenger_from_env({**chat, S.SLACK + "WORKSPACE": ""}) is None
    assert S.messenger_from_env({**chat, S.SLACK + "SHARED_CHANNELS": "C3"}) is None
    assert S.mail_from_env({}) is None and S.messenger_from_env({}) is None
