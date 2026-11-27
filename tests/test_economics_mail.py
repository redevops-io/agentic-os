"""E-mail and internal-messenger adapters: roles from addresses, customers only by explicit identifiers, threads from headers, one message per Message-ID, strict reads, access scopes."""
from email.message import EmailMessage

from agentic_os.collaboration.slack import SlackCollaborationProvider, SlackError
from agentic_os.economics import evidence, mail
from agentic_os.economics.contracts import Extraction

IDENT = evidence.IdentityMap.from_customers([{"name": "Granite Works", "email_id": "buyer@granite-works.example", "website": "granite-works.example"},
                                             {"name": "Harbor Dental", "email_id": "office@harbor-dental.example", "website": "harbor-dental.example"}])
OWN = {"meridian.example"}


def eml(frm, to, subject, body, mid, date="Mon, 06 Oct 2026 09:00:00 +0000", cc="", irt="", refs=""):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"], m["Date"] = frm, to, subject, mid, date
    if cc:
        m["Cc"] = cc
    if irt:
        m["In-Reply-To"] = irt
    if refs:
        m["References"] = refs
    m.set_content(body)
    return m.as_bytes()


def parse(raw, scope="mailbox:sales@meridian.example/INBOX"):
    return mail.parse_message(raw, mailbox="sales@meridian.example", uid="1", tenant="t", identity=IDENT, own_domains=OWN, scope=scope)


def test_roles_come_from_the_addresses_and_the_customer_only_from_an_explicit_identifier():
    a = parse(eml("Buyer <buyer@granite-works.example>", "sales@meridian.example", "Pilot", "Can you confirm the pilot rate?", "<a@x>"))
    b = parse(eml("sales@meridian.example", "buyer@granite-works.example", "Re: Pilot", "Confirmed.", "<b@x>", irt="<a@x>"))
    c = parse(eml("dana@meridian.example", "sales@meridian.example", "Granite", "Dana here: ok the lower rate for Granite.", "<c@x>"))
    assert (a.ref.role, a.customer, a.identity_method) == ("customer", "Granite Works", "email")
    assert (b.ref.role, b.customer) == ("company", "Granite Works")
    assert c.ref.role == "internal_note" and c.customer is None                     # a name alone never resolves
    assert b.ref.thread == "<a@x>" and a.ref.thread == "<a@x>"                       # threads from headers, not subjects


def test_an_internal_message_that_writes_exactly_one_customers_address_resolves_and_two_do_not():
    one = parse(eml("dana@meridian.example", "sales@meridian.example", "Rate", "Please give buyer@granite-works.example the pilot rate.", "<d@x>"))
    assert (one.customer, one.identity_method) == ("Granite Works", "mentioned_identifier")
    two = parse(eml("dana@meridian.example", "sales@meridian.example", "Rates", "buyer@granite-works.example and office@harbor-dental.example both get it.", "<e@x>"))
    assert two.customer is None and "more than one" in two.identity_note


def test_quoted_text_is_cut_so_a_reply_does_not_repeat_the_statement_it_quotes():
    body = "Yes, 15% is fine.\n\nOn Mon, 6 Oct 2026, Buyer <buyer@granite-works.example> wrote:\n> Can we have 15% off?\n> Thanks"
    c = parse(eml("sales@meridian.example", "buyer@granite-works.example", "Re: terms", body, "<f@x>"))
    assert "Can we have" not in c.text and "15% is fine" in c.text


class FakeImap:
    def __init__(self, msgs, fail=None):
        self.msgs, self.fail, self.selected_readonly = msgs, fail or {}, None

    def login(self, u, p):
        if "login" in self.fail:
            raise OSError("auth")

    def select(self, folder, readonly=False):
        self.selected_readonly = readonly
        return ("OK", [b"1"])

    def uid(self, cmd, *a):
        if cmd == "SEARCH":
            return ("OK", [b" ".join(str(i).encode() for i in self.msgs)])
        uid = int(a[0])
        return ("NO", []) if uid in self.fail.get("fetch", ()) else ("OK", [(b"x", self.msgs[uid])])

    def logout(self):
        pass


def test_the_mailbox_is_read_read_only_a_failed_message_is_a_gap_and_a_failed_login_is_a_gap_not_an_empty_mailbox():
    msgs = {1: eml("buyer@granite-works.example", "sales@meridian.example", "Hi", "Please confirm the pilot rate.", "<1@x>"), 2: eml("buyer@granite-works.example", "sales@meridian.example", "Hi2", "x", "<2@x>")}
    ex, fake = Extraction(), FakeImap(msgs, {"fetch": (2,)})
    out = mail.collect_imap("h", "sales@meridian.example", "pw", ex, tenant="t", identity=IDENT, own_domains=OWN, imap_factory=lambda: fake)
    assert len(out) == 1 and fake.selected_readonly is True and len(ex.gaps) == 1 and "uid 2" in ex.gaps[0]["source"]
    assert out[0].ref.access_scope == "mailbox:sales@meridian.example/INBOX"
    ex2 = Extraction()
    assert mail.collect_imap("h", "u@meridian.example", "pw", ex2, tenant="t", identity=IDENT, own_domains=OWN, imap_factory=lambda: FakeImap({}, {"login": 1})) == [] and ex2.gaps
    ex3 = Extraction()
    assert mail.collect_imap("h", 'u"x', "pw", ex3, tenant="t", identity=IDENT, own_domains=OWN, folder='IN"BOX', imap_factory=lambda: FakeImap({})) == [] and ex3.gaps


def test_the_same_email_read_from_two_mailboxes_is_one_message_and_the_other_scope_is_kept():
    raw = eml("buyer@granite-works.example", "sales@meridian.example", "Hi", "text", "<same@x>")
    a, b = parse(raw, "mailbox:sales@meridian.example/INBOX"), parse(raw, "mailbox:dana@meridian.example/INBOX")
    out, dup = mail.dedupe([a, b])
    assert len(out) == 1 and dup == {a.ref.id: ["mailbox:dana@meridian.example/INBOX"]}


def test_a_mailbox_a_private_note_and_a_channel_are_never_covered_by_a_broader_grant():
    a = parse(eml("buyer@granite-works.example", "sales@meridian.example", "Hi", "x", "<1@x>"), "mailbox:sales@meridian.example/INBOX")
    b = parse(eml("buyer@granite-works.example", "dana@meridian.example", "Hi", "y", "<2@x>"), "mailbox:dana@meridian.example/INBOX")
    ok, withheld = evidence.visible_to([a, b], {"mailbox:sales@meridian.example/INBOX"})
    assert ok == [a] and withheld == 1
    assert evidence.visible_to([a], {"mailbox:sales@meridian.example"})[0] == []          # a prefix is not a grant
    assert evidence.INTERNAL_SUFFIX == "/internal"


def _slack(pages, replies=None, fail=False):
    calls = []

    def transport(method, url, token, payload):
        calls.append(url.rsplit("/", 1)[1])
        if fail:
            return {"ok": False, "error": "channel_not_found"}
        if url.endswith("conversations.history"):
            return pages[len(calls) and (payload.get("cursor") or "")]
        return {"ok": True, "messages": replies or []}
    return SlackCollaborationProvider("tok", transport=transport), calls


def test_messenger_history_pages_through_threads_stays_internal_and_a_cut_or_failed_read_is_never_complete():
    pages = {"": {"ok": True, "has_more": True, "response_metadata": {"next_cursor": "c1"},
                  "messages": [{"ts": "1790000100.0", "user": "U2", "text": "Granite wants the pilot rate again; ok?", "reply_count": 1, "thread_ts": "1790000100.0"}]},
             "c1": {"ok": True, "has_more": False, "messages": [{"ts": "1790000000.0", "user": "U1", "text": "Dana: lower pilot rate for buyer@granite-works.example is fine."}]}}
    replies = [{"ts": "1790000100.0", "user": "U2", "text": "root", "thread_ts": "1790000100.0"}, {"ts": "1790000200.0", "user": "U1", "text": "Yes, go ahead.", "thread_ts": "1790000100.0"}]
    p, calls = _slack(pages, replies)
    ex = Extraction()
    out = mail.collect_messenger(p, ["C1"], ex, tenant="t", identity=IDENT, users={"U1": "dana@meridian.example"}, own_domains=OWN, workspace="meridian")
    assert [c.text.split(".")[0][:12] for c in out] == ["Dana: lower ", "Granite want", "Yes, go ahea"]
    assert all(c.ref.role == "internal_note" for c in out) and out[0].customer == "Granite Works"
    assert out[0].ref.access_scope == "messenger:slack/meridian/channel:C1" and out[1].ref.thread == out[2].ref.thread
    p2, _ = _slack({}, fail=True)
    ex2 = Extraction()
    assert mail.collect_messenger(p2, ["C9"], ex2, tenant="t", identity=IDENT) == [] and "channel_not_found" in ex2.gaps[0]["what"]
    never_ending = {"": {"ok": True, "has_more": True, "response_metadata": {"next_cursor": "c"}, "messages": []}, "c": {"ok": True, "has_more": True, "response_metadata": {"next_cursor": "c"}, "messages": []}}
    p3, _ = _slack(never_ending)
    try:
        p3.read_history("C1", max_pages=3)
        raise AssertionError("a history that never ends must not be returned")
    except SlackError as e:
        assert "history_incomplete" in str(e)
