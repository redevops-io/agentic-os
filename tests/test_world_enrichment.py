"""Contact enrichment + compliance (suppression ledger, physical address, unsubscribe). Offline: no network."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentic_os.world import (  # noqa: E402
    ApolloProvider,
    OutreachContext,
    OutreachDecision,
    SuppressionLedger,
    decide,
    handle_unsubscribe,
    render_email,
    resolve_contact,
    unsubscribe_token,
    unsubscribe_url,
)
from agentic_os.world import enrichment as E  # noqa: E402


def test_no_provider_configured_returns_no_email(monkeypatch):
    for v in ("APOLLO_API_KEY", "HUNTER_API_KEY", "CLEARBIT_API_KEY", "REDEVOPS_ENRICHMENT_PROVIDER"):
        monkeypatch.delenv(v, raising=False)
    r = resolve_contact(domain="acme.com", titles=["Head of AI"])
    assert r["email"] is None and "no enrichment provider" in r["reason"]   # never fabricates an address


def test_apollo_people_search_resolves_verified_contact(monkeypatch):
    # Apollo's real flow: api_search returns person *ids* with name/email LOCKED, then people/match unlocks
    # the professional email (1 credit). resolve_contact must chain the two.
    def fake_post(url, payload, headers, timeout=10.0):
        if "mixed_people/api_search" in url:
            assert payload["q_organization_domains"] == "acme.com"
            return {"people": [{"id": "p-1", "title": "Head of AI Platform",
                                "name": None, "email": None, "email_status": None}]}
        if url.endswith("/people/match"):
            assert payload["id"] == "p-1" and payload["reveal_professional_emails"] is True
            return {"person": {"first_name": "Jordan", "name": "Jordan Lee", "title": "Head of AI Platform",
                               "email": "jordan@acme.com", "email_status": "verified"}}
        raise AssertionError("unexpected url " + url)
    monkeypatch.setattr(E, "_post", fake_post)
    r = resolve_contact(domain="acme.com", titles=["Head of AI Platform"], provider=ApolloProvider(key="k"))
    assert r["email"] == "jordan@acme.com" and r["verified"] is True and r["first_name"] == "Jordan"


def test_title_keywords_makes_roles_apollo_matchable():
    from agentic_os.world import title_keywords
    # human-readable buying-group roles -> loose keyword tokens Apollo People Search actually matches
    assert title_keywords(["Head of AI Platform", "VP Engineering"]) == ["ai", "platform", "engineering"]
    assert title_keywords(["CTO"]) == ["cto"]                       # nothing to drop -> falls back to the token


def test_source_apollo_list_reads_curated_prospects(monkeypatch):
    # the Chrome-extension bridge: a human saves LinkedIn prospects into an Apollo List "GTM-Runtime";
    # the pipeline reads that list via the API (no browser automation) and gets ready-to-govern contacts.
    from agentic_os.world import source_apollo_list

    def fake_get(url, headers=None, timeout=10.0):
        assert url.endswith("/labels")
        return {"labels": [{"id": "lst-9", "name": "GTM-Runtime"}, {"id": "lst-1", "name": "Other"}]}

    def fake_post(url, payload, headers, timeout=10.0):
        assert url.endswith("/contacts/search") and payload["label_ids"] == ["lst-9"]
        return {"contacts": [{"name": "Sam Rivera", "first_name": "Sam", "title": "Head of Platform",
                              "email": "sam@acme.dev", "email_status": "verified",
                              "organization": {"name": "Acme", "primary_domain": "acme.dev"}}]}

    monkeypatch.setattr(E, "_get", fake_get)
    monkeypatch.setattr(E, "_post", fake_post)
    people = source_apollo_list("GTM-Runtime", provider=ApolloProvider(key="k"))
    assert len(people) == 1 and people[0]["email"] == "sam@acme.dev" and people[0]["verified"] is True
    assert people[0]["company"] == "Acme" and people[0]["domain"] == "acme.dev"
    # a missing list yields [] (never fabricates), so the pipeline simply has nothing to send
    monkeypatch.setattr(E, "_get", lambda *a, **k: {"labels": []})
    assert source_apollo_list("Nope", provider=ApolloProvider(key="k")) == []


def test_verified_contact_unblocks_send(monkeypatch):
    # a lead with a verified email + grounded evidence + auto → SEND (the enrichment unblocks the gate)
    ctx = OutreachContext(company="Acme", first_name="Jordan", role="Head of AI",
                          verified_email="jordan@acme.com",
                          observed_activity="hiring around agent sandboxing",
                          specific_evidence_sentence="Your careers page lists an agent-infrastructure role "
                                                     "covering sandboxing and permission scoping.",
                          runtime_problem="execution policy is re-implemented per system",
                          evidence_about_company="the hiring", specific_problem_or_workflow="coding agents")
    assert decide(ctx, auto_send=True) is OutreachDecision.SEND


def test_compliance_footer_has_address_and_unsubscribe():
    ctx = OutreachContext(company="Acme", verified_email="a@acme.com", role="CTO",
                          observed_activity="building agents",
                          specific_evidence_sentence="Your repo shows an internal agent runtime under active work.",
                          runtime_problem="context is rebuilt per app")
    body = render_email(ctx)["body"]
    assert "20200 West Dixie Highway, Suite 902, Miami, Florida 33180" in body
    assert "redevops.io/unsubscribe?u=" in body and "10 business days" in body


def test_suppression_ledger_blocks_send(tmp_path, monkeypatch):
    path = str(tmp_path / "suppression.txt")
    led = SuppressionLedger(path=path)
    ctx = OutreachContext(company="Acme", verified_email="a@acme.com", role="CTO",
                          observed_activity="building agents",
                          specific_evidence_sentence="Your repo shows an internal agent runtime under active work.",
                          runtime_problem="context is rebuilt per app")
    assert decide(ctx, auto_send=True, ledger=led) is OutreachDecision.SEND        # not yet suppressed
    # a monitored unsubscribe records the opt-out; a fresh ledger over the same file honors it
    handle_unsubscribe(unsubscribe_token("a@acme.com"), led)
    assert SuppressionLedger(path=path).is_suppressed("a@acme.com")
    assert decide(ctx, auto_send=True, ledger=SuppressionLedger(path=path)) is OutreachDecision.SUPPRESSED
    # suppressing a whole domain works too
    led2 = SuppressionLedger(path=str(tmp_path / "s2.txt")); led2.suppress("acme.com")
    assert led2.is_suppressed("anyone@acme.com")


def test_unsubscribe_token_is_stable_and_pathless():
    t = unsubscribe_token("A@Acme.com ")
    assert t == unsubscribe_token("a@acme.com") and len(t) == 24        # normalized + stable
    assert "@" not in unsubscribe_url("a@acme.com")                     # raw address never in the link
