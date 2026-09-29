"""Cross-site content portfolio (Content & Search Intelligence §46–§53). Offline.

Each site is scanned in isolation on its own Umami data; the portfolio aggregates the per-site queues and
ranks the top opportunities across the whole set. Unknown sites resolve to an empty (honest) queue.
"""
from __future__ import annotations

from agentic_os.content.portfolio import Site, scan_portfolio, scan_site, sites_from_umami
from agentic_os.integrations.gsc import GscClient
from agentic_os.integrations.umami import UmamiClient

# canned Umami: two sites, one with a clear top page, one flat
_DATA = {
    "id-redevops": [{"x": "/hot", "y": 400}, {"x": "/a", "y": 100}, {"x": "/b", "y": 100}, {"x": "/cold", "y": 5}],
    "id-quantify": [{"x": "/x", "y": 50}, {"x": "/y", "y": 50}, {"x": "/z", "y": 50}],
}


class _StubUmami(UmamiClient):
    def __init__(self):
        super().__init__(base_url="http://stub", website_id="")

    def token(self):
        return "t"

    def websites(self):
        return [{"id": "id-redevops", "domain": "redevops.io"},
                {"id": "id-quantify", "domain": "quantify.club"}]

    def website_id_for(self, domain):
        return {"redevops.io": "id-redevops", "quantify.club": "id-quantify"}.get(domain, "")

    def top_pages(self, *, days=30, limit=50):
        return _DATA.get(self.website_id, [])


def test_sites_from_umami_discovers_properties():
    sites = sites_from_umami(_StubUmami())
    assert {s.domain for s in sites} == {"redevops.io", "quantify.club"}
    assert all(s.origin == f"https://{s.domain}" for s in sites)


def test_scan_site_is_isolated_and_scoped():
    r = scan_site(_StubUmami(), Site("redevops.io", "redevops.io"), min_median=1.0)
    assert r["resolved"] is True and r["pages"] == 4
    # /hot is a leverage opportunity; every action parks on approval
    assert r["queue"]["count"] >= 1 and r["queue"]["requires_approval"] == r["queue"]["count"]
    assert any(d["candidate_id"].startswith("content:") and "/hot" in d["page"]
               for d in r["queue"]["decisions"])


def test_unknown_site_resolves_to_empty_queue():
    r = scan_site(_StubUmami(), Site("nope", "nope.example"))
    assert r["resolved"] is False and r["pages"] == 0 and r["queue"]["count"] == 0


def test_portfolio_aggregates_and_ranks_across_sites():
    c = _StubUmami()
    p = scan_portfolio(c, sites_from_umami(c), min_median=1.0)
    assert p["sites"] == 2 and p["sites_with_data"] == 2
    assert p["total_pages"] == 7
    assert p["total_actions"] == sum(row["actions"] for row in p["per_site"])
    # the top opportunity across the portfolio comes from redevops.io (quantify is flat → no actions)
    assert p["top_opportunities"][0]["domain"] == "redevops.io"
    assert {d["domain"] for d in p["top_opportunities"]} == {"redevops.io"}
    # per-site table is sorted by action count (most opportunities first)
    counts = [row["actions"] for row in p["per_site"]]
    assert counts == sorted(counts, reverse=True)


def test_flat_site_yields_no_actions():
    # quantify.club is flat (all 50) → no leverage/underperformer signals → honest empty queue
    r = scan_site(_StubUmami(), Site("quantify.club", "quantify.club"), min_median=1.0)
    assert r["pages"] == 3 and r["queue"]["count"] == 0


class _StubGsc(GscClient):
    def __init__(self):
        super().__init__(key_file="/dev/null")

    def _bearer(self):
        return "t"

    def sites(self):
        return ["sc-domain:redevops.io"]

    def search_analytics(self, site_url, *, days=28, row_limit=1000, dimensions=("query", "page")):
        # a strong ranking with CTR far under baseline → CTR_OPPORTUNITY
        return [{"keys": ["governed runtime", "https://redevops.io/runtime"], "clicks": 10,
                 "impressions": 800, "ctr": 0.0125, "position": 3.0}]


def test_scan_site_merges_behavior_and_search_signals():
    r = scan_site(_StubUmami(), Site("redevops.io", "redevops.io"), min_median=1.0, gsc=_StubGsc())
    assert r["gsc_property"] == "sc-domain:redevops.io" and r["resolved"] is True
    signals = {d["signal"] for d in r["queue"]["decisions"]}
    assert "CTR_OPPORTUNITY" in signals               # from GSC
    assert "HIGH_TRAFFIC_LEVERAGE" in signals          # from Umami behavior — one merged queue
    assert r["queue"]["requires_approval"] == r["queue"]["count"]


def test_site_with_only_gsc_resolves_without_umami():
    # a domain GSC knows but Umami doesn't → still resolved, search signals only
    r = scan_site(_StubUmami(), Site("redevops.io", "notinumami.example"), gsc=_StubGsc())
    assert r["pages"] == 0
    # notinumami.example has no GSC property either → empty but self-skips cleanly
    assert r["resolved"] is False and r["queue"]["count"] == 0
