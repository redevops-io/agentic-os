"""Behavior signals from Umami analytics (Content & Search Intelligence plan §5).

GSC tells you how a page performs in search; Umami tells you how it performs once people arrive. This turns a
window of `AnalyticsObservation`s (Umami page behavior) into `BehaviorSignal`s that flow into the SAME content
approval queue as the search signals (`content.interventions`): a page pulling far below its peers is a
*fix/refresh* opportunity, and a page pulling far above them is a *leverage* opportunity. Deterministic and
explainable — the reference is the site's own median, so it self-calibrates per site.

`BehaviorSignal` mirrors `SearchSignal`'s shape (signal_type / affected_pages / confidence / status /
proposed_action / evidence / site_id), so `from_content_signal` consumes either without special-casing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from statistics import median
from typing import List, Optional, Tuple


class BehaviorSignalType(str, Enum):
    UNDERPERFORMING_PAGE = "UNDERPERFORMING_PAGE"
    HIGH_TRAFFIC_LEVERAGE = "HIGH_TRAFFIC_LEVERAGE"


@dataclass(frozen=True)
class BehaviorSignal:
    signal_type: BehaviorSignalType
    affected_pages: Tuple[str, ...]
    confidence: float
    status: str                          # PROPOSE (act) | WATCH (observe more)
    proposed_action: str
    evidence: Tuple[str, ...] = field(default_factory=tuple)
    site_id: str = ""


def underperforming_page(obs, *, site_median: float, floor_ratio: float = 0.3,
                         min_median: float = 20.0) -> Optional[BehaviorSignal]:
    """A page with real but weak traffic — below `floor_ratio` of the site median — is a refresh/promote
    opportunity. Needs a site with enough traffic (median ≥ min_median) for the comparison to mean anything."""
    if site_median < min_median or obs.pageviews <= 0 or obs.pageviews >= floor_ratio * site_median:
        return None
    ratio = obs.pageviews / site_median
    conf = round(min(0.9, 0.5 + 0.4 * (1.0 - ratio / floor_ratio)), 3)
    return BehaviorSignal(
        BehaviorSignalType.UNDERPERFORMING_PAGE, (obs.page_url,), conf, "PROPOSE",
        proposed_action="Refresh / optimize / promote this page — traffic is well below the site median",
        evidence=(f"umami:{obs.pageviews}v vs median {site_median:g}",), site_id=obs.site_id)


def high_traffic_leverage(obs, *, site_median: float, mult: float = 3.0,
                          min_median: float = 20.0) -> Optional[BehaviorSignal]:
    """A page pulling well above its peers (≥ `mult`× the site median) is a leverage opportunity — expand the
    topic, add internal links, or add a conversion path to compound a proven winner."""
    if site_median < min_median or obs.pageviews < mult * site_median:
        return None
    conf = round(min(0.9, 0.5 + 0.1 * (obs.pageviews / site_median - mult)), 3)
    return BehaviorSignal(
        BehaviorSignalType.HIGH_TRAFFIC_LEVERAGE, (obs.page_url,), conf, "PROPOSE",
        proposed_action="Leverage this high-traffic page — expand the topic, add internal links / a "
                        "conversion path",
        evidence=(f"umami:{obs.pageviews}v vs median {site_median:g}",), site_id=obs.site_id)


def scan_behavior(observations, *, min_median: float = 20.0) -> List[BehaviorSignal]:
    """Run the behavior detectors over a window of AnalyticsObservations, calibrated to the site's own median
    pageviews. Empty (no signal) when the site has too little traffic to compare against."""
    obs = list(observations)
    views = [o.pageviews for o in obs if o.pageviews > 0]
    if not views:
        return []
    site_median = float(median(views))
    out: List[BehaviorSignal] = []
    for o in obs:
        for sig in (underperforming_page(o, site_median=site_median, min_median=min_median),
                    high_traffic_leverage(o, site_median=site_median, min_median=min_median)):
            if sig is not None:
                out.append(sig)
    return out
