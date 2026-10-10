"""Market scan composition — observe → detect → match as one callable (Content/Search loop).

Proves scan_market chains the three pure stages: a stub fetcher over two competitors yields cross-company
patterns and matches a reversible opportunity against our own (no-trial) site; and the no-fetcher scan is empty.
"""
from __future__ import annotations

from agentic_os.market import MarketObservations, TrackedCompany, scan_market
from agentic_os.market.contracts import CTA, Offer, PageSnapshot, Provenance
from agentic_os.market.observe import FetchedPage

_TRIAL = """<html><head><title>Acme</title></head><body>
<p>Start your free trial today.</p><a href="/signup">Start free trial</a> <a href="/demo">Book a demo</a>
</body></html>"""


class _Fetcher:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url):
        return self.pages.get(url, FetchedPage(url=url, status=404))


def _co(ref, domain):
    return TrackedCompany(prov=Provenance("seed", ref), name=ref, domain=domain,
                          category="ai-agents", role="competitor")


def _own_without_trial():
    # our site: a landing + demo CTA, but NO free-trial offer → the trial pattern is a gap for us
    return MarketObservations(
        companies=(TrackedCompany(prov=Provenance("own", "us"), name="Us", domain="redevops.io"),),
        snapshots=(PageSnapshot(prov=Provenance("website", "us"), company_ref="us",
                                url="https://redevops.io", page_kind="home", title="Us"),),
        ctas=(CTA(prov=Provenance("website", "us"), page_ref="us", text="Book a demo", action="demo"),))


def test_scan_detects_pattern_and_matches_opportunity():
    pages = {f"https://{d}": FetchedPage(f"https://{d}", 200, _TRIAL) for d in ("a.example", "b.example")}
    scan = scan_market([_co("a", "a.example"), _co("b", "b.example")],
                       fetcher=_Fetcher(pages), own=_own_without_trial(), site="redevops.io")
    assert not scan.observations.is_empty()
    assert scan.patterns, "two competitors sharing a free-trial offer should form a pattern"
    assert scan.opportunities, "the trial pattern is a gap vs our no-trial site → an opportunity"


def test_no_fetcher_is_empty_but_safe():
    scan = scan_market([_co("a", "a.example")], site="redevops.io")
    assert scan.patterns == () and scan.opportunities == ()
    # companies are still recorded even with no fetcher (observe returns companies-only)
    assert scan.observations.counts().get("companies", 0) == 1
