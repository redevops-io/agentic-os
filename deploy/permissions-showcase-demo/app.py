"""Data-access permissions plane showcase for the demo (demo.redevops.io/permissions).

A standalone surface that shows the row/column **grants** plane. It exposes TWO distinct interfaces:

1. **The PUBLIC DEMO API** (`/api/demo/*`, no credential): a fixed list of scenarios evaluated over a SYNTHETIC table by a throw-away
   in-memory plane (`agentic_os.permissions_demo`). A visitor picks a scenario id and nothing else: never an identity, a role, a
   resource or an action. It never returns a real grant, a principal, a row predicate, an authorisation diagnostic or store status,
   and there is no per-visitor state (no cookie, no simulated writes).

2. **The ADMIN API** (`/api/permissions/*`: grant list, status, preview, authorize, create, update, remove): EVERY route requires the
   admin credential (`PERMISSIONS_ADMIN_KEY`, via `X-API-Key` or `Authorization: Bearer`, compared in constant time). It fails closed:
   no key configured means the routes are DISABLED (503), never open; a missing, empty or wrong credential is 401; a credential in the
   query string is refused (400).

The real grant store (`agentic_os.permissions.GrantStore`, encrypted at rest, fail-closed) is only ever touched by the admin API. It is
seeded once with two example grants so an administrator has something to look at.
"""
from __future__ import annotations

import logging
import threading

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from agentic_os import permissions as _perm
from agentic_os import permissions_demo as _demo
from agentic_os import views
from agentic_os.permissions_http import (SecurityHeaders as _SecurityHeaders, warn_if_unprotected_throttle as _warn_throttle, create_kwargs as _create_kwargs, json_object as _json_obj,
                                         require_permissions_admin as _require_admin, secure_page as _secure_page, update_fields as _update_fields)

_log = logging.getLogger("agentic_os.showcase")
_warn_throttle()

# No /docs, /redoc or /openapi.json (the admin routes are not advertised), and no trailing-slash redirects (a 404, never a Host-derived Location).
app = FastAPI(title="ReDevOps — Data-Access Permissions Plane", docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
app.add_middleware(_SecurityHeaders, no_store_prefixes=("/api/permissions",))

_SEED = ([
    {"id": "seed-support-crm-customers", "subject_kind": "app", "subject": "support", "resource_kind": "table",
     "resource_name": "crm.customers", "actions": ["read"], "row_scope": "in", "row_column": "region", "row_values": ["us"],
     "masked_columns": ["email"], "created_by": "seed"},
    {"id": "seed-finance-crm-customers", "subject_kind": "role", "subject": "finance", "resource_kind": "table",
     "resource_name": "crm.customers", "actions": ["read"], "row_scope": "all", "masked_columns": ["notes"], "created_by": "seed"},
])
_STORE = None
_STORE_LOCK = threading.Lock()


def _grants() -> "_perm.GrantStore":
    """The real grant store, built on FIRST USE by an authenticated admin request (never at import, never for the public routes): the public
    demo starts and serves with no PERMISSIONS_KEY at all. A store that cannot be opened is a 503, not a crash of the app."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            try:
                store = _perm.GrantStore()
                store.seed(_SEED)
            except _perm.GrantStorageError as e:
                _log.error("the grant store is unavailable, so the admin API answers 503: %s", e)
                raise HTTPException(503, "the grant store is unavailable on this server") from None
            _STORE = store
        return _STORE


require_admin = _require_admin          # the ONE admin dependency (PERMISSIONS_ADMIN_KEY only, 24+ characters): agentic_os.permissions_http


# ───────────────────────── the public demo API: synthetic, allowlisted, no state ─────────────────────────


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}            # liveness only: nothing about the store, its key or its grants


@app.get("/permissions", response_class=HTMLResponse)
def permissions_page() -> HTMLResponse:
    return _secure_page(views.permissions_demo_page, _demo.list_scenarios())


@app.get("/api/demo/scenarios")
def demo_scenarios() -> list[dict]:
    return _demo.list_scenarios()


@app.get("/api/demo/scenarios/{scenario_id}")
def demo_scenario(scenario_id: str) -> dict:
    if not _demo.valid_scenario_id(scenario_id):
        raise HTTPException(400, "not a scenario id")
    if scenario_id not in _demo.SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    return _demo.evaluate(scenario_id)


# ───────────────────────── the admin API: every route needs the admin credential ─────────────────────────


@app.get("/api/permissions/status", dependencies=[Depends(require_admin)])
def permissions_status() -> dict:
    return _grants().status()


@app.get("/api/permissions/grants", dependencies=[Depends(require_admin)])
def permissions_grants() -> list[dict]:
    return [g.to_dict() for g in _grants().list()]


@app.post("/api/permissions/grants", dependencies=[Depends(require_admin)])
async def permissions_add(request: Request) -> dict:
    b = await _json_obj(request)
    if not b.get("subject") or not b.get("resource_name"):
        raise HTTPException(400, "subject and resource_name are required")
    store = await run_in_threadpool(_grants)
    try:
        g = await run_in_threadpool(
            store.add,
            b.get("subject_kind", "app"), b["subject"], b.get("resource_kind", "table"), b["resource_name"],
            created_by="admin", **_create_kwargs(b))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return g.to_dict()


@app.put("/api/permissions/grants/{gid}", dependencies=[Depends(require_admin)])
async def permissions_update(gid: str, request: Request) -> dict:
    """Update-by-id (POST only ever creates)."""
    b = await _json_obj(request)
    store = await run_in_threadpool(_grants)
    try:
        g = await run_in_threadpool(store.update, gid, **_update_fields(b))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    if g is None:
        raise HTTPException(404, f"no grant {gid}")
    return g.to_dict()


@app.delete("/api/permissions/grants/{gid}", dependencies=[Depends(require_admin)])
def permissions_remove(gid: str) -> dict:
    try:
        return {"removed": _grants().remove(gid)}
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/permissions/preview", dependencies=[Depends(require_admin)])
async def permissions_preview(request: Request) -> dict:
    b = await _json_obj(request)
    return _perm.preview(await run_in_threadpool(_grants), b.get("subject_kind", "app"), b.get("subject", ""))


@app.post("/api/permissions/authorize", dependencies=[Depends(require_admin)])
async def permissions_authorize(request: Request) -> dict:
    b = await _json_obj(request)
    ident = _perm.Identity(app=b.get("app", ""), user=b.get("user", ""), roles=b["roles"] if "roles" in b else [])
    return _perm.PermissionsPlane(await run_in_threadpool(_grants)).authorize(ident, b.get("resource_kind", "table"), b.get("resource_name", ""),
                                                     b.get("action", "read")).to_dict()
