"""HTTP surface of the drill-down: three GET routes, nothing else.

    GET /api/projects/{project_id}/missions/{mission_id}/timeline?after_seq=&limit=&expect_lineage=&upto=
    GET /api/projects/{project_id}/missions/{mission_id}/evidence
    GET /api/projects/{project_id}/missions/{mission_id}/evidence/{evidence_id}

401 only when no valid credential is presented. After that, an unknown mission, a mission of another project and a missing
grant are one and the same 404 with one body. 400 (parameters) is only ever reached AFTER authorization, so it cannot be
used to probe for a mission. 304 is only answered to a caller who is authorized on this very request: a cached validator is
never a way around a revoked grant. 503 means the audit log could not be written and nothing was returned.
"""
# NB: no `from __future__ import annotations` - FastAPI must see real annotation objects on the route functions defined inside build_router().
import hashlib
import json
from typing import Any, Optional

from ..evidence_query.authz import Authenticator
from .fold import canonical
from .models import DrilldownRefused

_HEADERS = {"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
_ERRORS = {401: {"error": "authentication required"}, 404: {"detail": "Not Found"}, 400: {"error": "invalid request parameters"},
           503: {"error": "the audit log is unavailable; nothing was returned"}}


def build_router(service, authenticator: Authenticator):
    from fastapi import APIRouter, Request
    from fastapi.responses import Response

    router = APIRouter(prefix="/api/projects/{project_id}/missions/{mission_id}", tags=["drilldown"])

    def _json(status: int, body: Any, extra: Optional[dict] = None) -> Response:
        return Response(content=canonical(body), status_code=status, media_type="application/json", headers={**_HEADERS, **(extra or {})})

    def _principal(request: Request):
        h = request.headers.get("authorization", "")
        if not h.lower().startswith("bearer "):
            return None
        try:
            return authenticator.authenticate(h[7:].strip())
        except Exception:                                   # noqa: BLE001 - a broken authenticator authenticates nobody
            return None

    def _run(request: Request, call):
        principal = _principal(request)
        if principal is None:
            return _json(401, _ERRORS[401], {"WWW-Authenticate": "Bearer"})
        try:
            body = call(principal)
        except DrilldownRefused as e:
            return _json(e.status, _ERRORS[e.status])
        etag = 'W/"' + hashlib.sha256(canonical(body).encode()).hexdigest()[:32] + '"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={**_HEADERS, "ETag": etag})
        return _json(200, body, {"ETag": etag})

    @router.get("/timeline")
    def timeline(project_id: str, mission_id: str, request: Request, after_seq: str = "0", limit: str = "100",
                 expect_lineage: Optional[str] = None, upto: Optional[str] = None):
        return _run(request, lambda p: service.timeline(p, mission_id, project_id=project_id, after_seq=after_seq, limit=limit,
                                                       expect_lineage=expect_lineage, upto=upto))

    @router.get("/evidence")
    def evidence(project_id: str, mission_id: str, request: Request):
        return _run(request, lambda p: service.evidence_list(p, mission_id, project_id=project_id))

    @router.get("/evidence/{evidence_id}")
    def evidence_item(project_id: str, mission_id: str, evidence_id: str, request: Request):
        return _run(request, lambda p: service.evidence_inspect(p, mission_id, evidence_id, project_id=project_id))

    return router
