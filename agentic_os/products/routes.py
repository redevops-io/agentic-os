"""HTTP routes added to the Projects API when a products registry is configured.

``GET /api/products`` lists the registered products with their live status; the decision routes carry a person's click
to the product (``approvals.py``). Every write route here is ``human_only`` (``PRODUCT_ROUTE_POLICIES``): an agent or a
service credential never stands in for the person, and in the production profile Projects refuses an unauthenticated call
before the handler runs. Each route needs an identifiable principal (``principal.py``); there is no ``actor`` in any body.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException

from agentic_os.app_kit.route_authz import RoutePolicy as P

from . import approvals, qa as qamod, sentinel as sn
from .client import ProductError
from .hub import ProductHub
from .principal import current_principal
from .schemas import ACTION_ID, RUN_ID

_PID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_DIGEST = re.compile(r"^(sha256:)?[0-9a-f]{64}$")      # the product's own digest, as it reports it

PRODUCT_ROUTE_POLICIES = {
    "POST /api/products/{product_id}/actions/{request_id}/decision": P(
        "products.decide_action", "human_only",
        rationale="approving or rejecting a product action is a person's decision; the product re-checks it and executes separately"),
    "POST /api/products/{product_id}/actions/{request_id}/execute": P(
        "products.execute_action", "human_only",
        rationale="executing an approved product action is a second, separate human step"),
    "POST /api/products/{product_id}/runs/{run_id}/approval": P(
        "products.approve_run", "human_only",
        rationale="approving a quality run is a person's decision; the product binds it to the estimate digest"),
}


def _http(e: ProductError) -> HTTPException:
    status = e.status if e.status in (401, 403, 404, 409, 422, 429) else 503 if e.unavailable else 409 if e.code in ("conflict", "stale_approval") else 502
    if e.code == "stale_approval":
        status = 409
    if e.code == "no_approval_secret":
        status = 503
    return HTTPException(status, e.as_dict())


def _require_principal(hub: ProductHub) -> str:
    p = current_principal(hub.registry.single_owner)
    if not p:
        raise HTTPException(401, {"error": "principal_required",
                                  "detail": "Sign in to Projects so the product can attribute your decision to you."})
    return p


def _spec(hub: ProductHub, product_id: str, kind: str):
    spec = hub.spec(product_id) if _PID.match(product_id) else None
    if spec is None or spec.kind != kind:
        raise HTTPException(404, {"error": "not_found", "detail": "unknown product"})
    return spec


def public_status(st: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("id", "display_name", "kind", "ui_url", "version", "features", "unavailable", "health", "configured", "ready",
            "gate", "approval_secret", "info")
    out = {k: st[k] for k in keep if k in st}
    out["capabilities"] = sorted(_caps(st))
    return out


def _caps(st: Dict[str, Any]) -> list:
    if not st.get("ready"):
        return []
    from .schemas import INPUT_SCHEMAS
    return [c for c in INPUT_SCHEMAS if c.startswith(st["kind"] + ".")]


def make_router(hub: ProductHub) -> APIRouter:
    r = APIRouter()

    @r.get("/api/products")
    def _products() -> Dict[str, Any]:
        return {"products": [public_status(s) for s in hub.statuses()],
                "registry_errors": len(hub.registry.errors)}

    @r.get("/api/products/{product_id}/records/{record_id}")
    def _record(product_id: str, record_id: str) -> Dict[str, Any]:
        """The approval card of a product record (an Edge Sentinel action or a QA run), read fresh from the product."""
        spec = hub.spec(product_id) if _PID.match(product_id) else None
        if spec is None:
            raise HTTPException(404, {"error": "not_found", "detail": "unknown product"})
        try:
            if spec.kind == "security" and re.match(ACTION_ID, record_id):
                return sn.approval_card(spec, hub.sentinel(product_id).action(record_id, actor="projects:ui"))
            if spec.kind == "quality" and re.match(RUN_ID, record_id) and hub.ready(product_id):
                return qamod.approval_card(spec, hub.qa(product_id).run(record_id, actor="projects:ui"))
        except ProductError as e:
            raise _http(e)
        raise HTTPException(404, {"error": "not_found", "detail": "unknown record"})

    @r.post("/api/products/{product_id}/actions/{request_id}/decision")
    def _decision(product_id: str, request_id: str, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        spec = _spec(hub, product_id, "security")
        if not re.match(ACTION_ID, request_id):
            raise HTTPException(404, {"error": "not_found", "detail": "unknown action"})
        approved, digest = body.get("approved"), body.get("digest")
        extra = set(body) - {"approved", "digest", "rationale"}
        if extra or not isinstance(approved, bool) or not isinstance(digest, str) or not _DIGEST.match(digest) \
                or not isinstance(body.get("rationale", ""), str) or len(body.get("rationale", "")) > 200:
            raise HTTPException(422, {"error": "invalid", "detail": "body must be {approved: bool, digest: <64 hex>, rationale?: str<=200}"})
        principal = _require_principal(hub)
        try:
            out = approvals.sentinel_decision(hub, product_id, request_id, approved=approved, digest=digest,
                                              rationale=body.get("rationale", ""), principal=principal)
        except ProductError as e:
            raise _http(e)
        return {"product": spec.id, "request_id": request_id, "result": out,
                "source_refs": [f"{spec.id}:{request_id}"]}

    @r.post("/api/products/{product_id}/actions/{request_id}/execute")
    def _execute(product_id: str, request_id: str, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        spec = _spec(hub, product_id, "security")
        if not re.match(ACTION_ID, request_id):
            raise HTTPException(404, {"error": "not_found", "detail": "unknown action"})
        digest = body.get("digest")
        if set(body) - {"digest"} or not isinstance(digest, str) or not _DIGEST.match(digest):
            raise HTTPException(422, {"error": "invalid", "detail": "body must be {digest: <64 hex>}"})
        principal = _require_principal(hub)
        try:
            out = approvals.sentinel_execute(hub, product_id, request_id, digest=digest, principal=principal)
        except ProductError as e:
            raise _http(e)
        return {"product": spec.id, "request_id": request_id, "result": out,
                "source_refs": [f"{spec.id}:{request_id}"]}

    @r.post("/api/products/{product_id}/runs/{run_id}/approval")
    def _qa_approval(product_id: str, run_id: str, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        spec = _spec(hub, product_id, "quality")
        if not re.match(RUN_ID, run_id):
            raise HTTPException(404, {"error": "not_found", "detail": "unknown run"})
        approved, digest = body.get("approved"), body.get("estimate_digest")
        if set(body) - {"approved", "estimate_digest", "rationale"} or not isinstance(approved, bool) \
                or not isinstance(digest, str) or not _DIGEST.match(digest) \
                or not isinstance(body.get("rationale", ""), str) or len(body.get("rationale", "")) > 200:
            raise HTTPException(422, {"error": "invalid", "detail": "body must be {approved: bool, estimate_digest: <64 hex>, rationale?: str<=200}"})
        principal = _require_principal(hub)
        try:
            out = approvals.qa_approval(hub, product_id, run_id, approved=approved, estimate_digest=digest,
                                        rationale=body.get("rationale", ""), principal=principal)
        except ProductError as e:
            raise _http(e)
        return {"product": spec.id, "run_id": run_id, "result": out, "source_refs": [f"{spec.id}:{run_id}"]}

    return r


__all__ = ["make_router", "PRODUCT_ROUTE_POLICIES", "public_status"]
