"""HTTP routes of the dashboards (mounted by ``mount_dashboards`` from the Projects API).

Reads:   GET  /api/dashboards                      the catalog for this caller
         GET  /api/dashboards/views                the caller's saved views
         GET  /api/dashboards/{id}                 one dashboard (filters in the query)
         GET  /api/dashboards/{id}/drill           the records behind one number
Writes:  PUT  /api/dashboards/views                save or update a view (the caller's own)
         DELETE /api/dashboards/views/{view_id}
         POST /api/dashboards/{id}/track           hand a finding to the intake (never sends or approves)
         POST /api/dashboards/qa/results           report a test run (a service identity or the operator)
         DELETE /api/dashboards/qa/results         withdraw one run that was filed in the wrong place (the same callers)

Every route applies the caller's access (``access.py``) before any data is read back.
"""
from __future__ import annotations

import hmac
import re
from fnmatch import fnmatchcase
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Header, HTTPException, Request

from .access import Access
from .service import DashboardService, Forbidden, NotFound, parse_filters
from .store import StoreError

CAP_INGEST = "dashboards.ingest_qa"
_KEY = re.compile(r"^[^\x00-\x1f]{1,400}$")
_VIEW = re.compile(r"^view-[0-9a-f]{16}$")

#: The write routes' policies live with the rest of the Projects API's (agentic_os/projects_route_policy.py), so the one
#: completeness test covers them; this is the dashboards' subset.
def _policies() -> Dict[str, Any]:
    from agentic_os.projects_route_policy import PROJECTS_ROUTE_POLICIES  # noqa: PLC0415
    return {k: v for k, v in PROJECTS_ROUTE_POLICIES.items() if "/api/dashboards" in k}


DASHBOARD_ROUTE_POLICIES = _policies()


def _principal(svc: DashboardService) -> Optional[str]:
    from agentic_os.products.principal import current_principal  # noqa: PLC0415
    hub = getattr(svc.provider, "products_hub", None)
    return current_principal(hub.registry.single_owner if hub is not None else "")


def caller_access(svc: DashboardService) -> Access:
    return svc.access(_principal(svc))


def _session(request: Request, x_demo_session: Optional[str]) -> str:
    from agentic_os.projects_api import demo_client_key  # noqa: PLC0415
    return f"{demo_client_key(request)}|{(x_demo_session or '').strip()[:32]}"


def _owner(acc: Access, request: Request, x_demo_session: Optional[str]) -> str:
    """Saved views belong to the signed-in person. An anonymous demo visitor gets a sandbox keyed like Today's."""
    return f"user:{acc.principal}" if acc.principal else f"demo:{_session(request, x_demo_session)}"


def _authorize(svc: DashboardService, request: Request, x_demo_session: Optional[str]) -> "tuple[Access, str]":
    """The caller of a write route: its access and the owner key its views are stored under. A caller who has been
    granted no dashboard source at all (an ``access`` section or a tenant grant that leaves it nothing) is refused here,
    before anything is stored; in the production profile the route policy has already required a credential."""
    acc = caller_access(svc)
    if acc.grant.sources is not None and not acc.grant.sources:
        raise HTTPException(403, {"error": "forbidden", "detail": "you do not have access to the dashboards"})
    return acc, _owner(acc, request, x_demo_session)


def make_router(svc: DashboardService) -> APIRouter:
    r = APIRouter()

    def _filters(request: Request):
        return parse_filters(dict(request.query_params), now=svc.clock())

    def _guard(fn):
        try:
            return fn()
        except NotFound:
            raise HTTPException(404, {"error": "not_found", "detail": "no such dashboard, project or record list"})
        except Forbidden:
            raise HTTPException(403, {"error": "forbidden", "detail": "you do not have access to this dashboard or project"})
        except StoreError as e:
            raise HTTPException(e.status, e.as_dict())

    @r.get("/api/dashboards")
    def _catalog() -> Dict[str, Any]:
        acc = caller_access(svc)
        out = svc.catalog(acc)
        out["access"]["can_save"] = True
        return out

    @r.get("/api/dashboards/views")
    def _views(request: Request, dashboard: str = "", x_demo_session: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        acc = caller_access(svc)
        return {"views": svc.store.views(_owner(acc, request, x_demo_session), dashboard[:80]), "durable": svc.store.durable}

    @r.put("/api/dashboards/views")
    def _save_view(request: Request, body: Dict[str, Any] = Body(default={}), x_demo_session: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        acc, owner = _authorize(svc, request, x_demo_session)
        extra = set(body) - {"id", "name", "dashboard", "filters", "layout", "is_default"}
        dash, vid = body.get("dashboard"), body.get("id") or ""
        from .service import BY_ID  # noqa: PLC0415
        if extra or dash not in BY_ID or not isinstance(body.get("filters", {}), dict) or not isinstance(body.get("layout", {}), dict) \
                or not isinstance(body.get("is_default", False), bool) or (vid and not (isinstance(vid, str) and _VIEW.match(vid))):
            raise HTTPException(422, {"error": "invalid", "detail": "body must be {name, dashboard, filters?, layout?, is_default?, id?}"})
        f = body.get("filters") or {}
        if f.get("project") and not acc.project(str(f["project"])):
            raise HTTPException(403, {"error": "forbidden", "detail": "you do not have access to that project"})
        keep = {k: f[k] for k in ("project", "days", "since", "until", "product", "environment", "platform") if f.get(k) not in (None, "")}
        return _guard(lambda: svc.store.save_view(owner, name=body.get("name") or "", dashboard=dash, filters=keep,
                                                  layout=body.get("layout") or {}, is_default=bool(body.get("is_default", False)), view_id=vid, now=svc.clock()))

    @r.delete("/api/dashboards/views/{view_id}")
    def _delete_view(view_id: str, request: Request, x_demo_session: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        _acc, owner = _authorize(svc, request, x_demo_session)
        if not _VIEW.match(view_id) or not svc.store.delete_view(owner, view_id):
            raise HTTPException(404, {"error": "not_found", "detail": "no such view"})
        return {"deleted": view_id}

    def _require_reporter(x_service_token: Optional[str]) -> str:
        """Who may report (or withdraw) a test run: a service identity holding the capability, or the operator."""
        by = ""
        if x_service_token:
            from agentic_os.app_kit.route_authz import service_identities  # noqa: PLC0415
            for name, ident in service_identities().items():
                if hmac.compare_digest(x_service_token.encode(), ident["token"].encode()) and \
                        any(fnmatchcase(CAP_INGEST, c) for c in ident["capabilities"]):
                    by = f"service:{name}"
            if not by:      # a credential that was presented and did not verify is refused; it never falls back to who else the caller might be
                raise HTTPException(401, {"error": "unauthenticated", "detail": f"the service token is not valid or does not hold {CAP_INGEST}"})
        if not by:
            p = _principal(svc)
            hub = getattr(svc.provider, "products_hub", None)
            owner = hub.registry.single_owner if hub is not None else ""
            if p in ("operator", "admin", "local-owner") or (owner and p == owner):
                by = f"user:{p}"
        if not by:
            raise HTTPException(401, {"error": "unauthenticated",
                                      "detail": f"reporting test results needs a service identity holding {CAP_INGEST}, or the operator"})
        return by

    @r.post("/api/dashboards/qa/results")
    def _ingest(body: Dict[str, Any] = Body(default={}), x_service_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        """A CI lane reports one run: {project, suite, run_id, platform?, environment?, product?, at?, origin?, ref?, url?,
        evidence_verified?, results: [{test_id, status, severity?, detail?, requirements?, duration_ms?}]}. Statuses are
        passed | failed | unsupported | blocked; a test that did not run is left out and is then counted as not executed."""
        by = _require_reporter(x_service_token)
        if svc.config.projects and body.get("project") not in svc.config.projects:
            raise HTTPException(422, {"error": "unknown_project", "detail": "project is not declared in the dashboards file"})
        out = _guard(lambda: svc.store.ingest_qa(body, by=by, now=svc.clock()))
        svc.cache.invalidate()
        return {**out, "recorded_by": by}

    @r.delete("/api/dashboards/qa/results")
    def _withdraw(project: str = "", suite: str = "", platform: str = "", run_id: str = "",
                  x_service_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        """Remove one reported run that was filed in the wrong place (all four of project, suite, platform, run_id are
        required and must match exactly). The same callers that may report may withdraw; the answer and the log say who did."""
        by = _require_reporter(x_service_token)
        if not (project and suite and platform and run_id):
            raise HTTPException(422, {"error": "invalid", "detail": "project, suite, platform and run_id are all required"})
        n = _guard(lambda: svc.store.withdraw_qa(project, suite, platform, run_id))
        svc.cache.invalidate()
        import logging  # noqa: PLC0415
        logging.getLogger("agentic_os.dashboards").warning("qa run withdrawn: %s/%s/%s/%s (%d results) by %s", project, suite, platform, run_id, n, by)
        return {"withdrawn": {"project": project, "suite": suite, "platform": platform, "run_id": run_id}, "results": n, "withdrawn_by": by}

    @r.get("/api/dashboards/{dashboard_id}")
    def _dashboard(dashboard_id: str, request: Request, fresh: bool = False) -> Dict[str, Any]:
        acc = caller_access(svc)
        return _guard(lambda: svc.build(dashboard_id, _filters(request), acc, fresh=fresh).to_dict())

    @r.get("/api/dashboards/{dashboard_id}/drill")
    def _drill(dashboard_id: str, request: Request, key: str = "", limit: int = 200) -> Dict[str, Any]:
        if not _KEY.match(key):
            raise HTTPException(422, {"error": "invalid", "detail": "key is required"})
        acc = caller_access(svc)
        return _guard(lambda: svc.drill(dashboard_id, key, _filters(request), acc, limit=limit))

    @r.post("/api/dashboards/{dashboard_id}/track")
    def _track(dashboard_id: str, request: Request, body: Dict[str, Any] = Body(default={}),
               x_demo_session: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        key, note = body.get("key"), body.get("note", "")
        if set(body) - {"key", "note", "filters"} or not isinstance(key, str) or not _KEY.match(key) or not isinstance(note, str) \
                or not isinstance(body.get("filters", {}), dict):
            raise HTTPException(422, {"error": "invalid", "detail": "body must be {key, note?, filters?}"})
        acc, _own = _authorize(svc, request, x_demo_session)
        filters = parse_filters(body.get("filters") or {}, now=svc.clock())
        try:
            item = _guard(lambda: svc.track(dashboard_id, key, filters, acc, session=_session(request, x_demo_session), note=note))
        except NotImplementedError:
            raise HTTPException(501, {"error": "unsupported", "detail": "this deployment does not provide capture"})
        except Exception as e:  # noqa: BLE001  (a CaptureError carries its own status)
            status, as_dict = getattr(e, "status", None), getattr(e, "as_dict", None)
            if status and as_dict:
                raise HTTPException(status, as_dict())
            raise
        return {"tracked": True, "capture": item}

    return r


__all__ = ["make_router", "DASHBOARD_ROUTE_POLICIES", "caller_access", "CAP_INGEST"]
