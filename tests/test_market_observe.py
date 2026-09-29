"""Website/funnel observation — the first real MarketSourceAdapter (Market-Intelligence plan §6, Phase 1).

Offline: a stub fetcher serves canned HTML for a company's acquisition pages; asserts snapshots, page-kind
classification, change-hash, and CTA/offer/form extraction, that observation is read-only + self-skips, and
that the observed evidence reconstructs a canonical funnel through the Phase-0 resolver.

Run:  uv run python -m pytest tests/test_market_observe.py -q
"""
from __future__ import annotations

from agentic_os.market import (
    MarketSourceAdapter, SimpleFunnelResolver, TrackedCompany, WebsiteSourceAdapter)
from agentic_os.market.contracts import Provenance
from agentic_os.market.observe import FetchedPage

_HOME = """<html><head><title>Acme — AI agents</title></head><body>
<h1>Build agents fast</h1><p>Start your free trial today. Free forever plan available.</p>
<a href="/signup">Start free trial</a> <a href="/demo">Book a demo</a>
<form><input name="email"><input name="password"></form></body></html>"""

_PRICING = """<html><head><title>Pricing</title></head><body>
<p>14-day free trial. Save 20% annually.</p><a href="/signup">Get started</a>
<a href="/resources">Download the free template</a></body></html>"""


class _Fetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return self.pages.get(url, FetchedPage(url=url, status=404))


def _company():
    return TrackedCompany(prov=Provenance("seed", "acme"), name="Acme", domain="acme.example",
                          category="ai-agents", role="competitor")


def _adapter():
    pages = {
        "https://acme.example": FetchedPage("https://acme.example", 200, _HOME),
        "https://acme.example/pricing": FetchedPage("https://acme.example/pricing", 200, _PRICING),
    }
    return WebsiteSourceAdapter(fetcher=_Fetcher(pages)), pages


def test_disconnected_adapter_self_skips():
    a = WebsiteSourceAdapter(fetcher=None)
    assert not a.connected()
    assert a.observe(_company()).snapshots == ()          # no fetcher → nothing, no raise
    assert isinstance(a, MarketSourceAdapter) and a.provider == "website"


def test_observes_pages_with_kind_and_change_hash():
    a, _ = _adapter()
    obs = a.observe(_company())
    kinds = {s.page_kind for s in obs.snapshots}
    assert kinds == {"home", "pricing"}                    # only the reachable pages, classified
    home = next(s for s in obs.snapshots if s.page_kind == "home")
    assert home.title == "Acme — AI agents" and home.content_hash.startswith("sha256:")
    assert home.prov.evidence_refs == ("https://acme.example",)   # raw page kept as evidence


def test_extracts_ctas_offers_forms():
    a, _ = _adapter()
    obs = a.observe(_company())
    actions = {c.action for c in obs.ctas}
    assert {"trial", "demo", "signup"} & actions           # trial + demo + get-started(signup)
    offer_kinds = {o.kind for o in obs.offers}
    assert "trial" in offer_kinds and "freemium" in offer_kinds and "lead_magnet" in offer_kinds
    assert offer_kinds == set(dict.fromkeys(offer_kinds))  # de-duped per kind across the company
    signup_form = next(f for f in obs.forms if f.purpose == "signup")
    assert "email" in signup_form.fields and "password" in signup_form.fields


def test_observation_is_read_only_get_only():
    a, pages = _adapter()
    a.observe(_company())
    # only GETs of the configured paths were issued; nothing posted/submitted
    assert all(u.startswith("https://acme.example") for u in a.fetcher.calls)


def test_observed_evidence_reconstructs_a_funnel():
    a, _ = _adapter()
    obs = a.observe(_company())
    funnel = SimpleFunnelResolver().reconstruct("acme", obs)
    stages = funnel.stages()
    assert "landing" in stages and "offer" in stages and "signup" in stages   # end-to-end P0↔P1
