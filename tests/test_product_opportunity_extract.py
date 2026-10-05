"""Product Opportunity Intelligence Phase 1 — PainObservation evidence schema + workflow-pain extraction.

Proves: a first-person cross-app workflow complaint extracts into an immutable PainObservation (apps, frequency,
workaround, cross_app, bitemporal provenance); a feature wish / promo with no behavioural pain extracts to None;
and extraction is conservative + honest (evidence_strength is a proxy, higher with specificity).
"""
from __future__ import annotations

from agentic_os.agent_gateway.social.contracts import SocialObservation
from agentic_os.product_opportunity import PainObservation, extract_pain


def _obs(text, *, author="u1", published=1000, observed=2000, url="http://x", community="r/ecommerce"):
    return SocialObservation(provider="reddit", source_type="post", source_ref="t3_1", text=text,
                             author_ref=author, observed_at=observed, published_at=published,
                             retrieval_method="reddit-api", evidence_ref="ev:1",
                             provider_fields={"url": url, "community": community})


def test_cross_app_workflow_pain_extracts():
    o = _obs("Every morning I manually copy competitor prices from Amazon into a spreadsheet to decide ours.")
    p = extract_pain(o)
    assert isinstance(p, PainObservation)
    assert "amazon" in p.applications_mentioned and "spreadsheet" in p.applications_mentioned
    assert p.cross_app and p.frequency_hint == "daily"
    assert "manually" in p.manual_steps and p.source == "reddit"
    # bitemporal provenance: published (known_at) distinct from observed_at
    assert p.prov.known_at == 1000 and p.prov.observed_at == 2000
    assert p.author_hash.startswith("ah_") and p.evidence_strength > 0


def test_reconcile_across_apps_detected():
    o = _obs("We spend hours reconciling Stripe payouts against QuickBooks every week by hand.")
    p = extract_pain(o)
    assert p is not None and {"stripe", "quickbooks"} <= set(p.applications_mentioned)
    assert p.cross_app and p.frequency_hint == "weekly" and "reconcile" in p.current_workflow


def test_feature_wish_without_behaviour_is_none():
    assert extract_pain(_obs("It would be cool if Salesforce had a dark mode.")) is None
    assert extract_pain(_obs("Check out my new SaaS — 50% off this week!")) is None
    assert extract_pain(_obs("")) is None


def test_strength_rises_with_specificity_and_wtp():
    weak = extract_pain(_obs("manually update inventory, kind of tedious"))
    strong = extract_pain(_obs("Every day we manually copy orders from Shopify into NetSuite by hand; "
                               "we built a spreadsheet and even pay someone to keep them in sync."))
    assert weak is not None and strong is not None
    assert strong.evidence_strength > weak.evidence_strength
    assert strong.willingness_to_pay_signal and strong.workaround


def test_author_hash_is_stable_and_not_identity():
    a = extract_pain(_obs("I manually sync HubSpot and Zendesk every day", author="alice"))
    b = extract_pain(_obs("We manually export invoices from Xero every month", author="alice"))
    assert a.author_hash == b.author_hash and "alice" not in a.author_hash   # stable hash, not the identity
