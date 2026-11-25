"""Analytical dashboards for Projects.

Native, dependency-free projections over the systems a deployment already has connected: the registered products
(redevops-qa, Edge Sentinel), the Projects provider (missions, attention, runtime, context sources) and, when the
operator enables them, the business applications (Twenty, ERPNext, Chatwoot, Lago, Umami, Postiz, Listmonk) and
Control Tower's Metabase for analytical queries.

* ``contracts``  the typed projection (Dashboard / Panel / Metric / Record / SourceStatus)
* ``config``     the operator file: projects, attribution, QA traceability catalog, enabled sources, access
* ``sources``    one collector per system, reusing the kernel's existing clients; a shared short read cache
* ``store``      sqlite: metric history, saved views, reported QA results
* ``access``     who may see which projects and sources (no new authority)
* ``builders``   one per dashboard
* ``service``    catalog, build, drill-down, actions, the Sidekick answer
* ``routes``     the HTTP surface the Projects UI calls (``mount_dashboards``)

Metabase is optional. Nothing here imports it, pandas or a charting library; the desktop and WASM installations get
the same dashboards over whatever is connected.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def mount_dashboards(app: Any, provider: Any) -> Tuple[Any, Dict[str, Any]]:
    """Wire the dashboards into a Projects app. Returns ``(service, route policies)``."""
    from .routes import DASHBOARD_ROUTE_POLICIES, make_router
    from .service import DashboardService
    svc = DashboardService(provider)
    provider.dashboards_service = svc
    app.include_router(make_router(svc))
    app.state.dashboards = svc
    return svc, dict(DASHBOARD_ROUTE_POLICIES)


def sidekick_hook(provider: Any, ctx: Dict[str, Any], text: str) -> Optional[Dict[str, Any]]:
    """Answer a question asked from a dashboard (``ctx.objectRef`` = ``dashboard:<id>#<drill key>``) from the evidence
    behind that number. None when the message is not about a dashboard."""
    svc = getattr(provider, "dashboards_service", None)
    ref = str((ctx or {}).get("objectRef") or "")
    if svc is None or not ref.startswith("dashboard:"):
        return None
    t = (text or "").lower()
    if not any(w in t for w in ("explain", "why", "what", "evidence", "behind", "how", "which", "show")):
        return None                                  # an instruction ("plan a fix…") goes on to the Mission path
    from .routes import caller_access
    return svc.explain(ref, caller_access(svc))


__all__ = ["mount_dashboards", "sidekick_hook"]
