"""Funnel observation providers — run funnel diagnosis on REAL data, not baked constants (plan §7, P3 wiring).

The flagship capability was provider-neutral but only ever ran on synthetic inputs. This implements a real
`FunnelObservationProvider` over the existing env-configured Umami client: map a funnel's stage pages to Umami
pageviews and derive per-stage conversion, so `diagnose_funnel` runs on live behavior. ``stage_conversion_from_
pageviews`` is pure/deterministic; the Umami read degrades to empty on any failure (self-skip), so callers fall
back to synthetic when no source is configured. Umami is ONE provider — the same shape works for any metrics source.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional

from ..integrations.umami import UmamiClient, collect_page_behavior, umami_from_env
from .funnel import ConversionFunnel


def _stage_key(stage) -> str:
    return stage.resource_ref or stage.stage_id


def stage_conversion_from_pageviews(funnel: ConversionFunnel, pageviews: Mapping[str, int]) -> Dict[str, float]:
    """Per-stage conversion = fraction of this stage's viewers who reach the next stage (views_next/views_here),
    clamped to [0,1]; the terminal stage is 1.0. A low value marks where the funnel leaks. Deterministic.
    ``pageviews`` is keyed by a stage's resource_ref (page path) or its stage_id."""
    stages = funnel.stages
    out: Dict[str, float] = {}
    for i, s in enumerate(stages):
        v_here = pageviews.get(_stage_key(s), pageviews.get(s.stage_id, 0))
        if i + 1 < len(stages):
            nxt = stages[i + 1]
            v_next = pageviews.get(_stage_key(nxt), pageviews.get(nxt.stage_id, 0))
            out[s.stage_id] = round(min(1.0, v_next / v_here), 4) if v_here > 0 else 0.0
        else:
            out[s.stage_id] = 1.0
    return out


@dataclass
class UmamiFunnelProvider:
    """A FunnelObservationProvider backed by Umami page metrics."""
    client: UmamiClient

    def pageviews(self, *, days: int = 30) -> Dict[str, int]:
        views: Dict[str, int] = {}
        for obs in collect_page_behavior(self.client, days=days):
            views[obs.page_url] = views.get(obs.page_url, 0) + int(obs.pageviews)
        return views

    def stage_conversion(self, funnel: ConversionFunnel, *, days: int = 30) -> Dict[str, float]:
        return stage_conversion_from_pageviews(funnel, self.pageviews(days=days))


def funnel_observation(funnel: ConversionFunnel, *, client: Optional[UmamiClient] = None,
                       domain: str = "", days: int = 30) -> Optional[Dict[str, float]]:
    """Return per-stage conversion for a funnel from a live source, or None if no source is configured / reachable /
    has no data (so the caller can fall back to synthetic). Uses ``client`` if given, else ``umami_from_env()``. If
    the client has no ``website_id``, resolves it from ``domain`` (so callers need only a domain, not an id)."""
    c = client or umami_from_env()
    if c is None:
        return None
    if not getattr(c, "website_id", "") and domain:
        try:
            wid = c.website_id_for(domain)
        except Exception:  # noqa: BLE001
            wid = ""
        if not wid:
            return None
        c.website_id = wid
    conv = UmamiFunnelProvider(c).stage_conversion(funnel, days=days)
    # all non-terminal stages zero (empty Umami / paths don't match) → treat as no observation
    return conv if any(v for k, v in conv.items() if k != funnel.stages[-1].stage_id) else None


__all__ = ["stage_conversion_from_pageviews", "UmamiFunnelProvider", "funnel_observation"]
