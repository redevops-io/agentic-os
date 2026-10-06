"""Product Opportunity Intelligence Phase 2 — extraction enrichment + evidence independence.

Proves: actor + consequence are extracted; and independence counting collapses non-independent evidence
(same author, reposts/near-duplicate text, same source URL) so raw_mentions ≠ independent_evidence_count —
twenty copies of one viral post are one piece of evidence, ten independent operators are ten.
"""
from __future__ import annotations

from agentic_os.agent_gateway.social.contracts import SocialObservation
from agentic_os.product_opportunity import assess_independence, extract_pain


def _obs(text, *, sid="s1", author="u1", url="", community="r/ecommerce", published=1000):
    return SocialObservation(provider="reddit", source_type="post", source_ref=sid, text=text,
                             author_ref=author, observed_at=2000, published_at=published,
                             retrieval_method="reddit-api", evidence_ref="ev:" + sid,
                             provider_fields={"url": url, "community": community})


def _pain(text, **kw):
    return extract_pain(_obs(text, **kw))


# ── extraction enrichment ─────────────────────────────────────────────────────────────────────────────────
def test_actor_and_consequence_extracted():
    p = _pain("As an Amazon seller I manually copy prices from Amazon into a spreadsheet every day; "
              "we keep losing sales when ours is stale.")
    assert p.actor == "ecommerce_operator" and "lost_sales" in p.consequence


def test_bookkeeper_time_cost():
    p = _pain("Our bookkeeper spends hours every week manually reconciling Stripe payouts in QuickBooks.")
    assert p.actor == "bookkeeper" and "time_cost" in p.consequence


# ── independence / dedup ──────────────────────────────────────────────────────────────────────────────────
def test_independent_operators_count_separately():
    ps = [
        _pain("Every day I manually copy orders from Shopify into NetSuite by hand.", sid="a", author="u1"),
        _pain("We reconcile Stripe payouts against QuickBooks manually every week.", sid="b", author="u2"),
        _pain("I manually sync HubSpot contacts into Zendesk every morning.", sid="c", author="u3"),
    ]
    rep = assess_independence([p for p in ps if p])
    assert rep.raw_mentions == 3 and rep.independent_evidence_count == 3 and rep.author_count == 3


def test_same_author_collapses():
    ps = [
        _pain("Every day I manually copy orders from Shopify into NetSuite by hand.", sid="a", author="dana"),
        _pain("Also I manually export invoices from Xero into a spreadsheet each month.", sid="b", author="dana"),
    ]
    rep = assess_independence([p for p in ps if p])
    assert rep.raw_mentions == 2 and rep.independent_evidence_count == 1   # one person, one piece of evidence


def test_reposts_and_same_url_collapse():
    text = "Every day I manually copy competitor prices from Amazon into a spreadsheet to decide ours."
    ps = [
        _pain(text, sid="orig", author="u1", url="http://x/1"),
        _pain(text, sid="repost", author="u2", url="http://x/1"),      # same URL (link to original)
        _pain(text + " so annoying", sid="quote", author="u3"),        # near-identical text (quote)
    ]
    rep = assess_independence([p for p in ps if p])
    assert rep.raw_mentions == 3 and rep.independent_evidence_count == 1   # one underlying complaint
    assert len(rep.groups) == 1 and rep.representative_ids


def test_viral_vs_independent_mix():
    viral = "Zapier keeps breaking when I sync Stripe and QuickBooks every day."
    ps = [
        _pain(viral, sid="v1", author="a"),
        _pain(viral, sid="v2", author="b"),            # repost (near-dup)
        _pain(viral, sid="v3", author="c"),            # repost (near-dup)
        _pain("We manually re-key quotes from Salesforce into our billing system each time.", sid="u", author="d"),
    ]
    rep = assess_independence([p for p in ps if p])
    assert rep.raw_mentions == 4 and rep.independent_evidence_count == 2   # one viral cluster + one independent
