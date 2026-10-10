"""Market scan — the observe → detect → match composition as one callable (plan: Content/Search loop).

The observe/pattern/opportunity pieces existed as separate pure functions but nothing composed them at runtime
(the wiring-maturity audit found acquisition.market_observe had no handler). This is that composition: observe a
watch_list of competitors, detect cross-company patterns, and match them into reversible opportunities against our
own site. Observation is READ-ONLY and fully injectable — pass a real PageFetcher for live fetches, or none for a
deterministic/empty scan; consequential changes (experiments) still flow through the governed Executor elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from .contracts import MarketObservations, MarketPattern, Opportunity, TrackedCompany
from .observe import PageFetcher, WebsiteSourceAdapter
from .opportunities import match_opportunities
from .patterns import detect_patterns


@dataclass(frozen=True)
class MarketScan:
    """A full scan: the merged observations, the detected patterns, and the opportunities matched to our site."""
    observations: MarketObservations
    patterns: Tuple[MarketPattern, ...] = ()
    opportunities: Tuple[Opportunity, ...] = ()


def scan_market(watch_list: Sequence[TrackedCompany], *, fetcher: Optional[PageFetcher] = None,
                own: Optional[MarketObservations] = None, site: str = "", min_companies: int = 2,
                min_confidence: float = 0.0) -> MarketScan:
    """Observe each company in ``watch_list``, merge, detect patterns, and (when ``site`` and ``own`` are given)
    match opportunities. With no fetcher each observation is companies-only, so the scan is empty but never raises.
    Deterministic: observations merge in watch_list order; pattern/opportunity ordering is owned by their funcs."""
    adapter = WebsiteSourceAdapter(fetcher=fetcher)
    merged = MarketObservations()
    for company in watch_list:
        merged = merged.merge(adapter.observe(company))
    patterns = detect_patterns(merged, min_companies=min_companies)
    own_obs = own if own is not None else MarketObservations()
    opportunities = (match_opportunities(patterns, own_obs, site=site, min_confidence=min_confidence)
                     if site else ())
    return MarketScan(observations=merged, patterns=patterns, opportunities=opportunities)


__all__ = ["MarketScan", "scan_market"]
