"""Data-access permissions plane showcase for the demo (demo.redevops.io/permissions).

A standalone, v6-native surface: the row/column data-access **grants** plane that used to live inside the
retired v3 control-plane, extracted to its own service so demo.redevops.io stays v6-only. It depends ONLY
on the public kernel's permissions module + views — no control-plane, no fleet, no registry.

What it shows: a grant is (subject · resource · actions · row-scope · column-mask); the preview materializes
what each subject can actually read over a sample table. Grants persist to an encrypted-at-rest store
(AES-GCM; falls back to a generated on-box key in the demo) and FAIL CLOSED if the file can't be verified.
The WRITE path (add/remove grants) persists ONLY with a valid admin key (X-API-Key or Basic). Without one -- including when no key is
configured -- writes are SIMULATED in a per-visitor in-memory overlay (cookie-keyed, capped, 30 min TTL,
never persisted or shared), so the public demo stays interactive without mutating shared state.

It runs the REAL kernel plane (`agentic_os.permissions`) over seeded demo grants — the same store a client
app installs `permissions.make_authorizer` over — so the demo is not a mock. One router, HTML page + the
JSON endpoints the page calls.
"""
from __future__ import annotations

import base64
import hmac
import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from agentic_os import permissions as _perm
from agentic_os import permissions_overlay as _ov
from agentic_os import views

app = FastAPI(title="ReDevOps — Data-Access Permissions Plane")

# Subjects shown in the page's dropdowns — the v6 domain agents + common apps/roles. Cosmetic: the plane
# authorizes any (subject_kind, subject); these just populate the UI.
_SUBJECTS = [
    "revenue", "intelligence", "finance", "customer-success", "content",
    "security-compliance", "support", "billing", "books", "crm",
]
_ROLES = ["admin", "finance", "support", "analyst", "auditor"]

# The grant store (encrypted-at-rest; dev falls back to a generated on-box key). Seeded with the same two
# grants the control-plane shipped, so the preview shows a scoped read + a column-masked read out of the box.
_GRANTS = _perm.GrantStore()
_GRANTS.seed([
    {"subject_kind": "app", "subject": "support", "resource_kind": "table", "resource_name": "crm.customers",
     "actions": ["read"], "row_scope": "in", "row_column": "region", "row_values": ["us"],
     "masked_columns": ["email"], "created_by": "seed"},
    {"subject_kind": "role", "subject": "finance", "resource_kind": "table", "resource_name": "crm.customers",
     "actions": ["read"], "row_scope": "all", "masked_columns": ["notes"], "created_by": "seed"},
])


_OVERLAY = _ov.OverlayRegistry(_GRANTS)


def _is_admin(request: Request) -> bool:
    """True only for a request carrying the configured admin key (X-API-Key or HTTP Basic password).
    An UNSET key never means "open": it fails closed to the simulated per-session overlay."""
    expected = os.environ.get("PERMISSIONS_ADMIN_KEY") or os.environ.get("AGENTIC_OS_API_KEY") or ""
    if not expected:
        return False
    presented = request.headers.get("x-api-key") or ""
    if not presented:
        auth = request.headers.get("authorization") or ""
        if auth[:6].lower() == "basic ":
            try:
                presented = base64.b64decode(auth[6:]).decode("utf-8").partition(":")[2]
            except Exception:  # noqa: BLE001
                presented = ""
    return bool(presented) and hmac.compare_digest(presented.encode(), expected.encode())


def _sid(request: Request) -> str | None:
    return request.cookies.get(_ov.COOKIE_NAME)


def _view(request: Request):
    """Store the read/decision endpoints evaluate against: base + this visitor's simulated overlay."""
    return _OVERLAY.view(_sid(request))


def _simulated(request: Request, payload, sid: str) -> JSONResponse:
    resp = JSONResponse(payload, headers={"X-Demo-Simulated": "1"})
    resp.set_cookie(_ov.COOKIE_NAME, sid, max_age=_ov.SESSION_TTL_S, httponly=True, samesite="lax",
                    secure=(request.headers.get("x-forwarded-proto") or request.url.scheme) == "https",
                    path="/")
    return resp


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, **_GRANTS.status()}


@app.get("/permissions", response_class=HTMLResponse)
def permissions_page() -> HTMLResponse:
    return HTMLResponse(views.permissions_page(
        subjects=_SUBJECTS, roles=_ROLES,
        resource=_perm.PREVIEW_RESOURCE, columns=_perm.PREVIEW_COLS,
        subject_kinds=list(_perm.SUBJECT_KINDS), resource_kinds=list(_perm.RESOURCE_KINDS),
        actions=list(_perm.ACTIONS), row_scopes=list(_perm.ROW_SCOPES),
    ))


@app.get("/api/permissions/status")
def permissions_status(request: Request) -> dict:
    return _view(request).status()


@app.get("/api/permissions/grants")
def permissions_grants(request: Request) -> list[dict]:
    return [g.to_dict() for g in _view(request).list()]


@app.post("/api/permissions/grants")
async def permissions_add(request: Request) -> dict:
    b = await request.json()
    if not b.get("subject") or not b.get("resource_name"):
        raise HTTPException(400, "subject and resource_name are required")
    kw = dict(
        actions=b.get("actions") or ["read"], row_scope=b.get("row_scope", "all"),
        row_column=b.get("row_column", "owner"), row_values=b.get("row_values") or [],
        masked_columns=b.get("masked_columns") or [])
    if not _is_admin(request):
        try:
            sid, g = _OVERLAY.add(_sid(request), b.get("subject_kind", "app"), b["subject"],
                                  b.get("resource_kind", "table"), b["resource_name"], **kw)
        except ValueError as e:
            raise HTTPException(429, str(e))
        return _simulated(request, g.to_dict(), sid)
    try:
        g = _GRANTS.add(
            b.get("subject_kind", "app"), b["subject"], b.get("resource_kind", "table"), b["resource_name"],
            actions=b.get("actions") or ["read"], row_scope=b.get("row_scope", "all"),
            row_column=b.get("row_column", "owner"), row_values=b.get("row_values") or [],
            masked_columns=b.get("masked_columns") or [], created_by="admin")
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return g.to_dict()


@app.delete("/api/permissions/grants/{gid}")
def permissions_remove(gid: str, request: Request) -> dict:
    if not _is_admin(request):
        try:
            sid, removed = _OVERLAY.remove(_sid(request), gid)
        except ValueError as e:
            raise HTTPException(429, str(e))
        return _simulated(request, {"removed": removed}, sid)
    try:
        return {"removed": _GRANTS.remove(gid)}
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/permissions/preview")
async def permissions_preview(request: Request) -> dict:
    b = await request.json()
    return _perm.preview(_view(request), b.get("subject_kind", "app"), b.get("subject", ""))


@app.post("/api/permissions/authorize")
async def permissions_authorize(request: Request) -> dict:
    b = await request.json()
    ident = _perm.Identity(app=b.get("app", ""), user=b.get("user", ""), roles=b.get("roles") or [])
    plane = _perm.PermissionsPlane(_view(request))
    return plane.authorize(ident, b.get("resource_kind", "table"), b.get("resource_name", ""),
                           b.get("action", "read")).to_dict()
