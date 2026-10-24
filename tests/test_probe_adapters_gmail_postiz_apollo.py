"""Gmail, Postiz and Apollo reconciliation probes: what each provider lets a read-only lookup prove, and what it never does."""
from __future__ import annotations

import time

import pytest

from agentic_os.mission.probe_adapters import apollo, gmail, postiz
from agentic_os.mission.probe_adapters._common import SKEW_S, epoch, httpx_get
from agentic_os.mission.probes import ProbeContext, ProbeOutcome as O, ProbeRegistry, ProbeResult

NOW = 1_800_000_000.0
DISPATCHED = NOW - 600.0


def iso(ts):
    from agentic_os.mission.probe_adapters._common import iso_utc
    return iso_utc(ts)


class Fake:
    """Scripted provider: routes keyed by path (and optionally by a query hint). Records every request; a probe may only ever read."""

    def __init__(self, routes=None, fn=None):
        self.routes, self.fn, self.requests = routes or {}, fn, []

    def __call__(self, path, params):
        self.requests.append((path, dict(params)))
        if self.fn:
            r = self.fn(path, params)
            if isinstance(r, Exception):
                raise r
            return r
        hit = self.routes.get(path)
        if isinstance(hit, Exception):
            raise hit
        return hit or (404, {}, {})


def ctx(ref, dispatched=DISPATCHED, now=NOW):
    return ProbeContext(lease={"meta": {}}, attempt=1, now=now, dispatched_at=dispatched, effect_ref=ref)


# ═══════════════════════════ shared ═══════════════════════════
@pytest.mark.parametrize("status,hdr,outcome", [(429, {"Retry-After": "9"}, O.TRANSIENT_ERROR), (503, {}, O.TRANSIENT_ERROR), (401, {}, O.UNSUPPORTED), (403, {}, O.UNSUPPORTED)])
def test_every_adapter_maps_provider_failures_the_same_way(status, hdr, outcome):
    g = gmail.GmailProbes(Fake({"/users/me/messages": (status, None, hdr)}))
    p = postiz.PostizProbes(Fake({"/posts": (status, None, hdr)}))
    a = apollo.ApolloProbes(Fake({"/contacts/c1": (status, None, hdr)}))
    assert g.send(ctx({"to": "a@b.co", "subject": "Hi"})).outcome is outcome
    assert p.publish(ctx({"content_sha256": "x", "integrations": "i1"})).outcome is outcome
    assert a.enroll(ctx({"contact_ids": "c1", "sequence_id": "s1"})).outcome is outcome
    if status == 429:
        assert g.send(ctx({"to": "a@b.co", "subject": "Hi"})).retry_after_s == 9.0


def test_a_lookup_that_raises_is_transient_never_a_verdict():
    for probe, ref in ((gmail.GmailProbes(Fake(fn=lambda *_: TimeoutError("t"))).send, {"to": "a@b.co", "subject": "S"}),
                       (postiz.PostizProbes(Fake(fn=lambda *_: TimeoutError("t"))).publish, {"content_sha256": "x", "integrations": "i"}),
                       (apollo.ApolloProbes(Fake(fn=lambda *_: TimeoutError("t"))).enroll, {"contact_ids": "c", "sequence_id": "s"})):
        r = probe(ctx(ref))
        assert r.outcome is O.TRANSIENT_ERROR and "TimeoutError" in r.detail


def test_the_read_transport_refuses_anything_outside_its_read_paths():
    exact = httpx_get("https://example.invalid", {}, ("/posts",))
    for bad in ("/posts/123", "/posts/123/delete", "/integrations", "/postsx", "/"):
        with pytest.raises(PermissionError):
            exact(bad, {})
    one = httpx_get("https://example.invalid", {}, ("/contacts",), one_segment=True)
    for bad in ("/contacts/c1/delete", "/contacts/../x/y", "/people", "/contactsx", "/contacts/a b"):
        with pytest.raises(PermissionError):
            one(bad, {})


def test_no_adapter_can_claim_an_authoritative_negative():
    for q in (gmail.QUALIFICATION, postiz.QUALIFICATION, apollo.QUALIFICATION):
        assert q.authoritative_negative is False and q.eventually_consistent is True and q.visibility_window_s is None


def test_a_negative_claimed_by_any_of_them_is_downgraded_by_the_registry():
    reg = ProbeRegistry()
    claim = ProbeResult(O.CONFIRMED_NOT_EXECUTED, evidence={"kind": "provider_record", "reference": "searched"}, authoritative_negative=True)
    for q in (gmail.QUALIFICATION, postiz.QUALIFICATION, apollo.QUALIFICATION):
        v = reg.judge(q, claim, dispatched_at=DISPATCHED, now=NOW)
        assert v.outcome is O.INCONCLUSIVE and v.downgraded


# ═══════════════════════════ Gmail ═══════════════════════════
REF = {"to": "buyer@example.com", "subject": "Your invoice"}


def gmsg(mid, subject="Your invoice", to="Buyer <buyer@example.com>", sent=NOW - 300, msgid="<abc@mail>"):
    return {"id": mid, "threadId": "t-" + mid, "internalDate": str(int(sent * 1000)),
            "payload": {"headers": [{"name": "Subject", "value": subject}, {"name": "To", "value": to}, {"name": "Message-ID", "value": msgid}]}}


def gmail_fake(listed, messages):
    def fn(path, params):
        if path == "/users/me/messages":
            return 200, {"messages": [{"id": i} for i in listed]}, {}
        mid = path.rsplit("/", 1)[1]
        return (200, messages[mid], {}) if mid in messages else (404, {}, {})
    return Fake(fn=fn)


def test_gmail_searches_sent_mail_to_the_recipient_since_the_call_with_epoch_seconds():
    f = gmail_fake([], {})
    gmail.GmailProbes(f).send(ctx(REF))
    path, params = f.requests[0]
    assert path == "/users/me/messages"
    assert params["q"] == f'in:sent to:buyer@example.com subject:"Your invoice" after:{int(DISPATCHED - SKEW_S)}'


def test_one_verified_message_is_a_candidate_not_proof_by_default():
    p = gmail.GmailProbes(gmail_fake(["m1"], {"m1": gmsg("m1")}))
    r = p.send(ctx(REF))
    assert r.outcome is O.INCONCLUSIVE and r.provider_ref == "m1" and "unrelated" in r.detail and r.result is None


def test_an_unrelated_identical_message_is_never_silently_certain():
    """A person sent the same subject to the same recipient inside the window; our own send never landed."""
    p = gmail.GmailProbes(gmail_fake(["theirs"], {"theirs": gmsg("theirs", sent=DISPATCHED + 20)}))
    assert p.send(ctx(REF)).outcome is O.INCONCLUSIVE


def test_the_opt_in_confirms_one_verified_message_and_says_not_identified():
    p = gmail.GmailProbes(gmail_fake(["m1"], {"m1": gmsg("m1")}), accept_unique_match=True)
    r = p.send(ctx(REF))
    assert r.outcome is O.CONFIRMED_EXECUTED and r.result["id"] == "m1" and "not identified" in r.detail


def test_two_verified_messages_are_inconclusive_possible_duplicate():
    p = gmail.GmailProbes(gmail_fake(["a", "b"], {"a": gmsg("a"), "b": gmsg("b", sent=NOW - 200)}), accept_unique_match=True)
    r = p.send(ctx(REF))
    assert r.outcome is O.INCONCLUSIVE and "duplicate" in r.detail


@pytest.mark.parametrize("msg", [gmsg("m1", subject="Your invoice is overdue"), gmsg("m1", to="someone@else.org"), gmsg("m1", sent=DISPATCHED - 86400)], ids=["longer subject", "other recipient", "older"])
def test_a_search_hit_that_does_not_match_exactly_is_not_evidence(msg):
    p = gmail.GmailProbes(gmail_fake(["m1"], {"m1": msg}), accept_unique_match=True)
    assert p.send(ctx(REF)).outcome is O.INCONCLUSIVE


def test_no_hit_is_inconclusive_and_a_message_deleted_between_list_and_get_is_not_evidence():
    assert gmail.GmailProbes(gmail_fake([], {})).send(ctx(REF)).outcome is O.INCONCLUSIVE
    assert gmail.GmailProbes(gmail_fake(["gone"], {}), accept_unique_match=True).send(ctx(REF)).outcome is O.INCONCLUSIVE


def test_a_message_id_the_sender_set_identifies_the_message_exactly():
    ref = {**REF, "message_id": "<req-77@redevops>"}
    p = gmail.GmailProbes(gmail_fake(["m9"], {"m9": gmsg("m9", msgid="<req-77@redevops>")}))
    r = p.send(ctx(ref))
    assert r.outcome is O.CONFIRMED_EXECUTED and r.provider_ref == "m9" and "Message-ID" in r.evidence["reference"]
    assert p.http.requests[0][1]["q"] == "rfc822msgid:req-77@redevops"


def test_a_message_id_hit_whose_header_differs_does_not_confirm():
    ref = {**REF, "message_id": "req-77@redevops"}
    p = gmail.GmailProbes(gmail_fake(["m9"], {"m9": gmsg("m9", msgid="<other@mail>")}))
    assert p.send(ctx(ref)).outcome is O.INCONCLUSIVE


@pytest.mark.parametrize("ref", [{}, {"to": "a@b.co"}, {"subject": "S"}])
def test_gmail_without_recipient_and_subject_cannot_be_probed(ref):
    f = gmail_fake([], {})
    assert gmail.GmailProbes(f).send(ctx(ref)).outcome is O.UNSUPPORTED and f.requests == []


def test_gmail_ref_derivation_takes_one_recipient_and_refuses_to_guess_for_several():
    assert gmail.derive_ref({"to": "a@b.co", "subject": "S", "body": "secret text"}) == {"to": "a@b.co", "subject": "S"}
    assert gmail.derive_ref({"to": ["a@b.co", "c@d.co"], "subject": "S"}) == {"subject": "S"}
    assert gmail.derive_ref({"to": ["a@b.co"], "subject": "S"})["to"] == "a@b.co"


# ═══════════════════════════ Postiz ═══════════════════════════
CONTENT = "Launch day: <b>we shipped</b>"
DIGEST = postiz.content_digest(CONTENT)
PREF = {"content_sha256": DIGEST, "integrations": "chan1", "type": "now"}


def ppost(pid, chan="chan1", content=CONTENT, state="PUBLISHED", date=NOW - 300, error=None):
    return {"id": pid, "content": content, "publishDate": iso(date), "releaseURL": "https://x/1", "state": state, "error": error, "integration": {"id": chan, "providerIdentifier": "x"}}


def plist(*posts):
    return Fake({"/posts": (200, {"posts": list(posts)}, {})})


def test_postiz_normalizes_markup_and_whitespace_when_comparing_content():
    assert postiz.content_digest("Launch   day: we shipped") == postiz.content_digest("<p>Launch day:&nbsp;<b>we shipped</b></p>".replace("&nbsp;", " "))
    assert postiz.content_digest("a") != postiz.content_digest("b")


def test_postiz_makes_exactly_one_request_for_the_window_around_the_call():
    f = plist()
    postiz.PostizProbes(f).publish(ctx(PREF))
    assert len(f.requests) == 1 and f.requests[0][0] == "/posts"
    assert set(f.requests[0][1]) == {"startDate", "endDate"} and f.requests[0][1]["startDate"] < f.requests[0][1]["endDate"]


def test_one_published_post_is_a_candidate_not_proof_by_default():
    r = postiz.PostizProbes(plist(ppost("p1"))).publish(ctx(PREF))
    assert r.outcome is O.INCONCLUSIVE and r.provider_ref == "p1" and "unrelated" in r.detail


def test_an_unrelated_identical_post_inside_the_window_is_never_silently_certain():
    r = postiz.PostizProbes(plist(ppost("someone-elses", date=DISPATCHED + 10))).publish(ctx(PREF))
    assert r.outcome is O.INCONCLUSIVE


def test_the_opt_in_confirms_the_state_the_request_implies():
    p = postiz.PostizProbes(plist(ppost("p1")), accept_unique_match=True)
    r = p.publish(ctx(PREF))
    assert r.outcome is O.CONFIRMED_EXECUTED and r.result["postIds"] == ["p1"] and r.result["state"] == "PUBLISHED"


@pytest.mark.parametrize("kind,state,expect", [("now", "PUBLISHED", O.CONFIRMED_EXECUTED), ("now", "QUEUE", O.INCONCLUSIVE), ("now", "DRAFT", O.INCONCLUSIVE),
                                               ("schedule", "QUEUE", O.CONFIRMED_EXECUTED), ("schedule", "PUBLISHED", O.CONFIRMED_EXECUTED),
                                               ("schedule", "DRAFT", O.INCONCLUSIVE), ("draft", "DRAFT", O.CONFIRMED_EXECUTED), ("draft", "PUBLISHED", O.INCONCLUSIVE)])
def test_only_the_state_the_request_type_implies_can_confirm(kind, state, expect):
    date = NOW + 3600
    ref = {**PREF, "type": kind, **({"date": iso(date)} if kind != "now" else {})}
    post = ppost("p1", state=state, date=date if kind != "now" else NOW - 300)
    assert postiz.PostizProbes(plist(post), accept_unique_match=True).publish(ctx(ref)).outcome is expect


def test_a_post_in_error_is_never_a_success_even_with_the_opt_in():
    r = postiz.PostizProbes(plist(ppost("p1", state="ERROR", error="token expired")), accept_unique_match=True).publish(ctx(PREF))
    assert r.outcome is O.INCONCLUSIVE and "ERROR" in r.detail and "token expired" in r.detail


def test_postiz_requires_every_channel_and_names_the_missing_ones():
    ref = {**PREF, "integrations": "chan1,chan2"}
    r = postiz.PostizProbes(plist(ppost("p1")), accept_unique_match=True).publish(ctx(ref))
    assert r.outcome is O.INCONCLUSIVE and "chan2" in r.detail
    both = postiz.PostizProbes(plist(ppost("p1"), ppost("p2", chan="chan2")), accept_unique_match=True).publish(ctx(ref))
    assert both.outcome is O.CONFIRMED_EXECUTED and both.result["postIds"] == ["p1", "p2"]


def test_two_matching_posts_on_a_channel_are_inconclusive_possible_duplicate():
    r = postiz.PostizProbes(plist(ppost("p1"), ppost("p2", date=NOW - 200)), accept_unique_match=True).publish(ctx(PREF))
    assert r.outcome is O.INCONCLUSIVE and "duplicate" in r.detail


@pytest.mark.parametrize("post", [ppost("p1", content="something else"), ppost("p1", chan="other"), ppost("p1", date=DISPATCHED - 86400)], ids=["content", "channel", "older"])
def test_a_post_that_does_not_match_is_not_evidence(post):
    assert postiz.PostizProbes(plist(post), accept_unique_match=True).publish(ctx(PREF)).outcome is O.INCONCLUSIVE


def test_postiz_reads_a_bare_list_and_reports_an_unreadable_shape_as_inconclusive():
    f = Fake({"/posts": (200, [ppost("p1")], {})})
    assert postiz.PostizProbes(f, accept_unique_match=True).publish(ctx(PREF)).outcome is O.CONFIRMED_EXECUTED
    assert postiz.PostizProbes(Fake({"/posts": (200, {"weird": 1}, {})})).publish(ctx(PREF)).outcome is O.INCONCLUSIVE


@pytest.mark.parametrize("ref", [{}, {"integrations": "c"}, {"content_sha256": "x"}, {**PREF, "type": "bogus"}])
def test_postiz_without_content_and_channels_cannot_be_probed(ref):
    f = plist()
    assert postiz.PostizProbes(f).publish(ctx(ref)).outcome is O.UNSUPPORTED and f.requests == []


def test_postiz_ref_holds_a_digest_not_the_content_and_sorts_the_channels():
    r = postiz.derive_ref({"content": CONTENT, "integrations": ["b", "a"], "type": "schedule", "date": "2026-11-01T10:00:00Z", "media": ["x"]})
    assert r == {"type": "schedule", "content_sha256": DIGEST, "integrations": "a,b", "date": "2026-11-01T10:00:00Z"}
    assert CONTENT not in repr(r)


# ═══════════════════════════ Apollo ═══════════════════════════
AREF = {"contact_ids": "c1", "sequence_id": "seq1"}


def acontact(cid="c1", seqs=("seq1",), added=NOW - 300, labelled=True):
    return {"contact": {"id": cid, "emailer_campaign_ids": list(seqs),
                        "contact_campaign_statuses": [{"status": "active", "added_at": iso(added), **({"emailer_campaign_id": s} if labelled else {})} for s in seqs]}}


def afake(**contacts):
    return Fake({f"/contacts/{k}": (200, v, {}) for k, v in contacts.items()})


def test_apollo_confirms_a_membership_that_began_since_the_call_and_reads_only():
    f = afake(c1=acontact())
    r = apollo.ApolloProbes(f).enroll(ctx(AREF))
    assert r.outcome is O.CONFIRMED_EXECUTED and r.result["contact_ids"] == ["c1"] and r.provider_ref == "c1"
    assert f.requests == [("/contacts/c1", {})]


def test_a_membership_from_before_the_call_is_inconclusive_the_attempt_may_have_been_skipped():
    r = apollo.ApolloProbes(afake(c1=acontact(added=DISPATCHED - 86400))).enroll(ctx(AREF))
    assert r.outcome is O.INCONCLUSIVE and "skipped" in r.detail


def test_not_in_the_sequence_is_inconclusive_never_a_negative():
    r = apollo.ApolloProbes(afake(c1=acontact(seqs=("other",)))).enroll(ctx(AREF))
    assert r.outcome is O.INCONCLUSIVE
    assert apollo.ApolloProbes(Fake({})).enroll(ctx(AREF)).outcome is O.INCONCLUSIVE        # contact not found


def test_a_contact_in_several_sequences_picks_the_entry_labelled_for_ours():
    c = acontact(seqs=("other", "seq1"))
    c["contact"]["contact_campaign_statuses"][0]["added_at"] = iso(DISPATCHED - 86400)       # the OTHER sequence is old; ours is new
    assert apollo.ApolloProbes(afake(c1=c)).enroll(ctx(AREF)).outcome is O.CONFIRMED_EXECUTED


def test_unlabelled_statuses_across_several_sequences_are_not_guessed_at():
    r = apollo.ApolloProbes(afake(c1=acontact(seqs=("other", "seq1"), labelled=False))).enroll(ctx(AREF))
    assert r.outcome is O.INCONCLUSIVE and "cannot be identified" in r.detail


def test_one_unlabelled_status_counts_only_if_the_sequence_is_listed():
    assert apollo.ApolloProbes(afake(c1=acontact(labelled=False))).enroll(ctx(AREF)).outcome is O.CONFIRMED_EXECUTED
    other = acontact(seqs=("other",), labelled=False)
    assert apollo.ApolloProbes(afake(c1=other)).enroll(ctx(AREF)).outcome is O.INCONCLUSIVE


def test_every_contact_must_be_confirmed_and_a_partial_result_names_the_rest():
    ref = {"contact_ids": "c1,c2", "sequence_id": "seq1"}
    r = apollo.ApolloProbes(afake(c1=acontact("c1"))).enroll(ctx(ref))
    assert r.outcome is O.INCONCLUSIVE and "c2" in r.detail and "1 of 2" in r.detail
    ok = apollo.ApolloProbes(afake(c1=acontact("c1"), c2=acontact("c2"))).enroll(ctx(ref))
    assert ok.outcome is O.CONFIRMED_EXECUTED and ok.result["contact_ids"] == ["c1", "c2"]


def test_a_membership_with_no_start_time_is_not_confirmed():
    c = acontact()
    del c["contact"]["contact_campaign_statuses"][0]["added_at"]
    assert apollo.ApolloProbes(afake(c1=c)).enroll(ctx(AREF)).outcome is O.INCONCLUSIVE


@pytest.mark.parametrize("ref", [{}, {"contact_ids": "c1"}, {"sequence_id": "s"}])
def test_apollo_without_contacts_and_sequence_cannot_be_probed(ref):
    f = afake()
    assert apollo.ApolloProbes(f).enroll(ctx(ref)).outcome is O.UNSUPPORTED and f.requests == []


def test_apollo_refuses_to_check_more_contacts_than_the_hourly_limit_allows():
    ids = ",".join(f"c{i}" for i in range(apollo.MAX_CONTACTS + 1))
    assert apollo.ApolloProbes(afake()).enroll(ctx({"contact_ids": ids, "sequence_id": "s"})).outcome is O.UNSUPPORTED


def test_apollo_ref_takes_one_or_many_contacts_and_drops_a_list_too_long_to_record_whole():
    assert apollo.derive_ref({"contact_id": "c9", "sequence_id": "s"}) == {"contact_ids": "c9", "sequence_id": "s"}
    assert apollo.derive_ref({"contact_ids": ["b", "a"], "sequence_id": "s"})["contact_ids"] == "a,b"
    assert "contact_ids" not in apollo.derive_ref({"contact_ids": [f"contact-{i:05d}" for i in range(40)], "sequence_id": "s"})


# ═══════════════════════════ registration and effect_ref ═══════════════════════════
def test_registration_records_scalar_refs_only_never_secrets_and_never_the_body():
    reg = ProbeRegistry()
    gmail.register_gmail(reg, Fake({}))
    postiz.register_postiz(reg, Fake({}))
    apollo.register_apollo(reg, Fake({}))
    g = reg.effect_ref("email.message.send", {"to": "a@b.co", "subject": "S", "body": "private", "api_key": "k", "access_token": "t"})
    assert g == {"to": "a@b.co", "subject": "S"}
    p = reg.effect_ref("content.publish", {"content": CONTENT, "integrations": ["i1"], "api_key": "k"})
    assert p["content_sha256"] == DIGEST and "api_key" not in p and CONTENT not in repr(p)
    a = reg.effect_ref("outreach.enroll", {"contact_ids": ["c1"], "sequence_id": "s1"})
    assert a == {"contact_ids": "c1", "sequence_id": "s1"}


def test_a_derivation_that_raises_does_not_stop_a_dispatch():
    reg = ProbeRegistry()
    reg.register("x.y", lambda c: None, gmail.QUALIFICATION, ref_derive=lambda i: 1 / 0)
    assert reg.effect_ref("x.y", {"a": 1}) == {}


def test_derived_values_are_held_to_the_same_rules_as_declared_ones():
    reg = ProbeRegistry()
    reg.register("x.y", lambda c: None, gmail.QUALIFICATION, ref_derive=lambda i: {"password": "p", "long": "z" * 500, "obj": {"a": 1}, "ok": 3})
    r = reg.effect_ref("x.y", {})
    assert r == {"long": "z" * 200, "ok": 3}


def test_apollo_unsupported_capabilities_escalate_at_once():
    reg = ProbeRegistry()
    apollo.register_apollo(reg, Fake({}))
    assert reg.supported("outreach.enroll")
    for cap in ("contact.upsert", "outreach.sequence.configure", "outreach.sequence.activate"):
        assert not reg.supported(cap)


def test_the_effect_ref_a_probe_receives_is_the_one_derived_at_dispatch():
    """Dispatch derives from inputs; the probe is called with exactly that, so what the probe looks for is what was sent."""
    reg = ProbeRegistry()
    postiz.register_postiz(reg, plist(ppost("p1")), accept_unique_match=True)
    inputs = {"content": CONTENT, "integrations": ["chan1"], "type": "now"}
    ref = reg.effect_ref("content.publish", inputs)
    (_, probe), = reg.adapters_for("content.publish")
    assert probe(ctx(ref)).outcome is O.CONFIRMED_EXECUTED
