"""Cross-site content portfolio (Content & Search Intelligence plan §46–§53).

The content loop is single-site: signals → a governed approval queue for one property. This lifts it to the
whole portfolio — every property the org runs — while keeping each site's analysis **isolated**: a site is
scanned on its own data, its interventions are scoped to it (`source_app = "content:<site>"`), and the
portfolio only *aggregates* the per-site queues; it never mixes one site's pages into another's ranking.

The output is a serializable rollup a cross-site dashboard renders directly: per-site action counts + the top
opportunities across the whole portfolio (ranked by the same transparent priority), so an operator sees where
the recoverable attention is without opening twelve tabs. Every action still parks on approval.

Sites can be declared explicitly or discovered from Umami (`sites_from_umami`), so the portfolio tracks
whatever properties are registered in analytics.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agentic_os.content.behavior_signals import scan_behavior
from agentic_os.content.interventions import content_intervention_queue
from agentic_os.integrations.umami import collect_page_behavior
from agentic_os.priority_engine import PriorityPolicy


@dataclass(frozen=True)
class Site:
    """One property in the portfolio. `site_id` is the logical scope label; `domain` resolves it in Umami."""
    site_id: str
    domain: str
    origin: str = ""

    def resolved_origin(self) -> str:
        return self.origin or f"https://{self.domain}"


def sites_from_umami(client) -> List[Site]:
    """Discover the portfolio from the properties registered in Umami (domain → site)."""
    out: List[Site] = []
    for w in client.websites():
        dom = str(w.get("domain", "") or "")
        if dom:
            out.append(Site(site_id=dom, domain=dom, origin=f"https://{dom}"))
    return out


def _gsc_signals(gsc, site: Site, *, days: int) -> tuple:
    """(search signals, property) for a site from GSC, or ([], "") when unavailable/unmatched. Self-skips."""
    if gsc is None:
        return [], ""
    from agentic_os.integrations.gsc import (
        collect_search_observations, property_for_domain, search_signals_from_observations,
    )
    prop = property_for_domain(gsc, site.domain)
    if not prop:
        return [], ""
    obs = collect_search_observations(gsc, prop, days=days, site_id=site.site_id)
    return search_signals_from_observations(obs), prop


def scan_site(client, site: Site, *, days: int = 30, min_median: float = 20.0,
              policy: Optional[PriorityPolicy] = None, gsc=None) -> Dict[str, Any]:
    """Scan ONE site in isolation → its governed content queue, merging Umami **behavior** signals with (when
    a GSC client is supplied) Google Search Console **search** signals for the property. Scoped to this site;
    resolves the Umami website by domain. Both sensors are read-only and self-skip."""
    search_sigs, gsc_property = _gsc_signals(gsc, site, days=days)
    wid = client.website_id_for(site.domain)
    behavior_sigs: list = []
    pages = 0
    if wid:
        client.website_id = wid
        obs = collect_page_behavior(client, days=days, origin=site.resolved_origin(), site_id=site.site_id)
        pages = len(obs)
        behavior_sigs = scan_behavior(obs, min_median=min_median)
    resolved = bool(wid) or bool(gsc_property)
    queue = content_intervention_queue(
        list(behavior_sigs) + list(search_sigs), policy=policy, source_app=f"content:{site.site_id}")
    return {"site": site.site_id, "domain": site.domain, "pages": pages, "resolved": resolved,
            "gsc_property": gsc_property, "queue": queue}


def scan_portfolio(client, sites: List[Site], *, days: int = 30, min_median: float = 20.0,
                   policy: Optional[PriorityPolicy] = None, top_n: int = 10, gsc=None) -> Dict[str, Any]:
    """Scan every site in isolation and aggregate into a portfolio rollup: totals, a per-site table, and the
    top opportunities across the whole portfolio (ranked by priority). When a GSC client is supplied, each
    site's queue merges Umami behavior + Search Console signals. Serializable for a dashboard."""
    per_site = [scan_site(client, s, days=days, min_median=min_median, policy=policy, gsc=gsc) for s in sites]

    top: List[Dict[str, Any]] = []
    for r in per_site:
        for d in r["queue"]["decisions"]:
            top.append({**d, "site": r["site"], "domain": r["domain"]})
    top.sort(key=lambda d: d["priority"], reverse=True)

    table = [{"site": r["site"], "domain": r["domain"], "pages": r["pages"], "resolved": r["resolved"],
              "actions": r["queue"]["count"], "requires_approval": r["queue"]["requires_approval"],
              "watching": r["queue"]["watching"]} for r in per_site]
    table.sort(key=lambda t: t["actions"], reverse=True)

    return {
        "sites": len(sites),
        "sites_with_data": sum(1 for r in per_site if r["pages"] > 0),
        "total_pages": sum(r["pages"] for r in per_site),
        "total_actions": sum(r["queue"]["count"] for r in per_site),
        "total_requires_approval": sum(r["queue"]["requires_approval"] for r in per_site),
        "total_watching": sum(r["queue"]["watching"] for r in per_site),
        "top_opportunities": top[:top_n],
        "per_site": table,
    }
