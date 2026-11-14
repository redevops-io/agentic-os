"""Integrated standalone products (Edge Sentinel, redevops-qa) in Projects and Sidekick.

A pure-addition package: the Projects server calls :func:`mount_products` once from ``create_app`` and
``sidekick_reply`` calls :func:`sidekick_hook` once. With ``AGENTIC_OS_PRODUCTS`` unset nothing in the server changes.

* ``registry``: the operator file naming each product's API base, health probe, contract and credentials.
* ``client``: the server-to-server HTTP hop (dedicated per-product service tokens, no redirects, size-capped).
* ``hub``: registry + clients + health/info state + a short read cache that is never authoritative.
* ``sentinel`` / ``qa``: each product's own API as typed calls and the pure mapping to Projects rows.
* ``projections``: the provider mixin (Missions, Attention, Activity, Apps, Today) with explicit unavailable rows.
* ``approvals``: the single-use, action-digest-bound approval token flow (``agentic_os.mission.approval``).
* ``tools`` / ``schemas`` / ``chat``: typed Sidekick tools (domains ``security`` and ``quality``), their JSON Schemas,
  and the single Sidekick hook. Decide and execute are never tools.
* ``gates``: the owner's gate for the quality (redevops-qa) surfaces.

Reuse, not a parallel engine: Projects keeps no copy of any product record; every row names the product's own ids.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from .hub import ProductHub
from .registry import ProductRegistry, registry_from_env

_log = logging.getLogger("agentic_os.products")


def hub_for(provider: Any) -> Optional[ProductHub]:
    return getattr(provider, "products_hub", None)


def mount_products(app: Any, provider: Any) -> Tuple[Any, Dict[str, Any]]:
    """Wire the products into a Projects app. Returns ``(provider, extra route policies)``; the provider is the same
    object with the products mixin in front of its class, or untouched when no registry is configured."""
    reg: Optional[ProductRegistry] = registry_from_env()
    if reg is None:
        return provider, {}
    for err in reg.errors:
        _log.warning("products registry: %s", err)
    if not reg.products:
        return provider, {}
    from .principal import PrincipalMiddleware
    from .projections import mixin_over
    from .routes import PRODUCT_ROUTE_POLICIES, make_router
    from .tools import build_registry
    hub = ProductHub(reg)
    provider = mixin_over(provider, hub)
    provider.products_tools = build_registry(hub)
    app.add_middleware(PrincipalMiddleware)
    app.include_router(make_router(hub))
    app.state.products_hub = hub
    return provider, dict(PRODUCT_ROUTE_POLICIES)


def sidekick_hook(provider: Any, ctx: Dict[str, Any], text: str) -> Optional[Dict[str, Any]]:
    hub = hub_for(provider)
    if hub is None:
        return None
    from .chat import try_handle
    return try_handle(hub, getattr(provider, "products_tools"), ctx or {}, text or "")


def ui_config_extra(provider: Any) -> Dict[str, Any]:
    """An additive ``products`` section for ``GET /api/ui-config`` (the line that serves it): which products feed
    Missions, Attention, Activity and Apps, and that those rows are the products' own records. The UI's existing
    ``data_source`` / ``projections.basis`` are not touched: Overview, Workflows and Discovery keep their own basis."""
    hub = hub_for(provider)
    if hub is None:
        return {}
    return {"products": {"configured": [s.id for s in hub.specs()], "basis": "product_records",
                         "rows": ["missions", "attention", "activity", "apps", "today"],
                         "sample_rows_kept": bool(hub.registry.keep_sample_rows)}}


def extend_apps(provider: Any, apps: list) -> list:
    hub = hub_for(provider)
    if hub is None:
        return apps
    from .projections import extend_apps as _ext
    return _ext(hub, apps)


__all__ = ["mount_products", "sidekick_hook", "extend_apps", "ui_config_extra", "hub_for", "ProductHub", "ProductRegistry"]
