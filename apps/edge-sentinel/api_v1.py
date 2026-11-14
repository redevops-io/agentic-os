"""The Edge Sentinel API, version 1: the ONE API that the browser UI, Projects/Sidekick and scripts use.

Every route here needs a credential in EVERY profile (there is no anonymous read): a user session cookie, HTTP Basic against the
local user table, or the service credential for reads and staging. A decision or an execution is a human act: it needs a user with
the `operator` role, or the service credential together with an approval token that Projects minted for one click and that is bound
to a user the product admin linked to that Projects principal. Records are read from the durable store (history_store.py); every
state change goes through the same `live.*` functions the legacy routes use, so there is one set of rows.

Roles: viewer (reads) < operator (stage, decide, execute) < admin (users, actor links). The boundary with the Enterprise edition is
documented in docs/standalone/edge-sentinel.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import auth, core, fixture_mode, history_store, identity, ipguard, live, ratelimit
from .evidence import canonical_json
from .identity import IdentityError, Principal
from .incident import stable_alert
from .version import PRODUCT_VERSION

router = APIRouter(prefix="/api/v1")

HEADER_SERVICE = "x-control-token"
HEADER_APPROVAL = "x-approval-token"
OPERATOR_NAME = "edge-sentinel"
_REPLAY = None
_SYNC = {"ts": 0.0}


class ApiError(Exception):
    """Rendered by `error_handler` as {"error": code, "message": ..., ...} with the status and headers given."""

    def __init__(self, status: int, code: str, message: str, headers: "dict | None" = None, **extra):
        super().__init__(message)
        self.status, self.headers, self.body = status, headers or {}, {"error": code, "message": message, **extra}


def _err(status: int, code: str, message: str, **extra) -> ApiError:
    return ApiError(status, code, message, **extra)


async def error_handler(request: Request, exc: ApiError):
    return JSONResponse(exc.body, status_code=exc.status, headers=exc.headers)


# ── authentication (shared with the standalone middleware) ──────────────────────────────────────────────────

def authenticate(request: Request) -> Optional[Principal]:
    """Who is calling: a session cookie, Basic against the user table, or the service credential. None = nobody."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    ident = identity.get()
    if ident.count_users() == 0:
        identity.seed_from_environment()
    h = request.headers
    principal: Optional[Principal] = None
    tok = identity.cookie_token(h.get("cookie", ""))
    if tok:
        principal = ident.session(tok)
    if principal is None and h.get("authorization", "").startswith("Basic "):
        principal = ident.basic(h["authorization"])
    if principal is None and h.get(HEADER_SERVICE):
        principal = _service(h.get(HEADER_SERVICE, ""))
    if principal is not None:
        request.state.principal = principal
    return principal


def _service(token: str) -> Optional[Principal]:
    from agentic_os.mission import control_auth
    try:
        if not control_auth.any_token_configured():
            return None
        caller = control_auth.resolve_caller(None, token)
    except HTTPException:
        return None
    if caller.role == "approver":
        return None
    return Principal("service", "service", "", "service", extra={"tenant": caller.tenant})


def current(request: Request) -> Principal:
    p = authenticate(request)
    if p is None:
        raise ApiError(401, "unauthenticated", "sign in, or send credentials", headers={"WWW-Authenticate": 'Basic realm="Edge Sentinel"'})
    if request.method not in ("GET", "HEAD", "OPTIONS") and not identity.csrf_ok(p, request.method, request.headers):
        raise _err(403, "csrf_required", "a session needs the X-CSRF-Token header (and a matching Origin) on every write")
    return p


def _gate(tool: str) -> None:
    """The kernel's basic permissions plane (AGENTIC_OS_PERMISSIONS=1) still governs the instance: no grant for the tool, no data."""
    from agentic_os.app_gate import tool_denial
    reason = tool_denial("edge-sentinel", tool)
    if reason:
        raise _err(403, "permission_denied", str(reason), tool=tool)


def viewer(request: Request) -> Principal:
    p = current(request)
    _gate("threat_summary")
    if p.kind == "user" and not p.at_least("viewer"):
        raise _err(403, "forbidden", "no role")
    return p                                           # a service credential may read


def operator(request: Request) -> Principal:
    p = current(request)
    _gate("ban_ip")
    if p.kind == "service":
        return p                                       # staging only: the handlers refuse decisions
    if not p.at_least("operator"):
        raise _err(403, "role_required", "this needs the operator role", role="operator")
    return p


def admin(request: Request) -> Principal:
    p = current(request)
    if not p.at_least("admin"):
        raise _err(403, "role_required", "this needs the admin role", role="admin")
    return p


def _replay_guard():
    global _REPLAY
    if _REPLAY is None:
        from agentic_os.mission.approval import default_replay_guard
        _REPLAY = default_replay_guard()
    return _REPLAY


def human(request: Request, capability: str, inputs: dict) -> Principal:
    """A person: a session/Basic user with the operator role, or the service credential + a verified approval token for a LINKED user.
    A body field never names the actor."""
    p = current(request)
    _gate("ban_ip")
    if p.kind == "user":
        if not p.at_least("operator"):
            raise _err(403, "role_required", "deciding and executing need the operator role", role="operator")
        return p
    tok = request.headers.get(HEADER_APPROVAL, "")
    if not tok:
        raise _err(403, "human_decision_required", "a service credential cannot decide or execute; a person must")
    from agentic_os.mission.approval import verify_approval_token
    marker = verify_approval_token(None, tok, operator=OPERATOR_NAME, capability=capability, inputs=inputs, replay_guard=_replay_guard())
    if marker is None or not marker.get("approved"):
        raise _err(403, "approval_token_invalid", "the approval token did not verify (expired, replayed, or bound to different content)")
    external = str(marker.get("by") or "")
    linked = identity.get().linked_user(external)
    if linked is None:
        raise _err(403, "actor_not_linked", "this Projects user is not linked to an Edge Sentinel user; an admin must link them first", external_actor=external)
    if not linked.at_least("operator"):
        raise _err(403, "role_required", "the linked user lacks the operator role", role="operator")
    linked.via = "projects"
    return linked


def _body(data) -> dict:
    return data if isinstance(data, dict) else {}


def _sync(force: bool = False) -> None:
    if force or time.time() - _SYNC["ts"] > 4.0:
        _SYNC["ts"] = time.time()
        live.sync_cases()


def _hs() -> history_store.HistoryStore:
    try:
        hs = history_store.get()
        return hs
    except history_store.StoreUnavailable as e:
        raise _err(503, "store_unavailable", str(e)) from None


# ── info, posture, events, decisions ─────────────────────────────────────────────────────────────────────────

_NOT_WIRED = [
    ("dfir", "DFIR timelines are a library module (evidence acquisition from supplied bytes); no route accepts forensic artifacts in this release"),
    ("enrichment", "enrichment providers (DNS, RDAP, certificates, threat intel) are contracts without a live provider; none is configured"),
    ("evaluation", "the evaluation and learning harness runs in the test suite; it has no runtime input"),
    ("risk_bridge", "the risk handoff has no receiving service in this deployment (Agentic Compliance owns that side)"),
    ("ai_security", "AI-security assessment consumes runtime-event/v10 objects from the Mission Runtime; use agent-events for plain event ingestion"),
    ("cti_graph", "the CTI graph needs a TAXII or OpenCTI source; none is configured (IOC normalisation and ATT&CK mapping are available)"),
    ("detection_publish", "detection publishing has no consumer here; Sigma/YARA validation is available"),
]


@router.get("/info")
def info(request: Request, p: Principal = Depends(viewer)):
    activity = core.fetch_activity()
    demo = fixture_mode.status()
    unavailable = [{"feature": f, "code": "NOT_WIRED", "reason": r, "remedy": ""} for f, r in _NOT_WIRED]
    if not activity.get("connected"):
        unavailable.append({"feature": "events", "code": "CORE_UNREACHABLE", "reason": "CrowdSec is not reachable, so events, decisions and new cases cannot be read",
                            "remedy": "check CROWDSEC_LAPI_URL and that the CrowdSec container is healthy"})
    if demo["enabled"]:
        unavailable.append({"feature": "execution", "code": "DEMO_MODE", "reason": "EDGE_SENTINEL_DEMO serves synthetic data and refuses every external side effect",
                            "remedy": "unset EDGE_SENTINEL_DEMO to act on a real CrowdSec core"})
    if not auth.block_enabled():
        unavailable.append({"feature": "enforcement", "code": "DRY_RUN", "reason": "SENTINEL_BLOCK_ENABLED is not set: an approved action is recorded as a dry run and nothing is banned",
                            "remedy": "set SENTINEL_BLOCK_ENABLED=1 to enforce approved bans"})
    if not core.watcher_configured():
        unavailable.append({"feature": "alerts", "code": "NO_WATCHER_LOGIN", "reason": "no CrowdSec watcher login is configured: alerts need docker exec of cscli and bans use the bouncer key, which a real CrowdSec refuses for writes",
                            "remedy": "set CROWDSEC_MACHINE_ID and CROWDSEC_MACHINE_PASSWORD"})
    hs = _hs()
    return {"product": "edge-sentinel", "version": PRODUCT_VERSION, "build": os.environ.get("SENTINEL_BUILD", ""),
            "tenant_label": core.TENANT or "Edge Sentinel",
            "mode": {"demo": demo["enabled"], "block_enabled": auth.block_enabled(), "dry_run": (not auth.block_enabled()) or demo["enabled"],
                     "require_two_person": live.two_person_required()},
            "core": {"name": "crowdsec", "connected": bool(activity.get("connected")), "watcher": core.watcher_configured()},
            "store": hs.health(),
            "features": {"cases": True, "actions": True, "events": True, "hunt": True, "attack": True, "ioc": True, "detections": True,
                         "agent_events": True, "soc2": True, "users": True},
            "unavailable": unavailable,
            "auth": {"methods": ["session", "basic", "service", "approval"], "roles": list(identity.ROLES)},
            "you": p.public()}


@router.get("/posture")
def posture(p: Principal = Depends(viewer)):
    a = core.fetch_activity()
    demo = fixture_mode.status()["enabled"]
    return {"decisions_active": a["counts"]["decisions"], "alerts_total": a["counts"]["alerts"], "sources": a["counts"]["sources"],
            "by_severity": _by_severity(a["alerts"]), "by_scenario": a["top_scenarios"], "kpis": a["kpis"],
            "as_of": a.get("read_at_iso", ""), "source": "fixture" if demo else "live", "connected": a["connected"],
            "status": {"decisions": a["decisions_status"], "alerts": a["alerts_status"]}}


def _by_severity(alerts: list) -> dict:
    out: dict = {}
    for r in alerts:
        out[r.get("severity", "low")] = out.get(r.get("severity", "low"), 0) + 1
    return out


@router.get("/events")
def events(severity: str = "", scenario: str = "", source_ip: str = "", since: str = "", limit: int = 50, cursor: str = "", p: Principal = Depends(viewer)):
    """CrowdSec alerts, read-only, newest first, each linked to the case it became."""
    alerts, status = core.read_alerts()
    hs = _hs()
    items = []
    for a in alerts:
        stable = stable_alert(a)
        raw = canonical_json(stable)
        eid = "ev-" + hashlib.sha256(raw.encode()).hexdigest()[:16]
        src = a.get("source") or {}
        ip = src.get("value") or src.get("ip") or ""
        sev = core._severity(a.get("scenario", ""))
        case = hs.one("SELECT case_id FROM case_evidence WHERE artifact_id=? LIMIT 1", (eid,))
        items.append({"event_id": "evt-" + hashlib.sha256(raw.encode()).hexdigest()[:16], "at": a.get("created_at", ""), "scenario": a.get("scenario", ""),
                      "source_ip": ip, "severity": sev, "events_count": a.get("events_count", 0),
                      "origin": "fixture" if fixture_mode.status()["enabled"] else "live", "case_id": case["case_id"] if case else None})
    if severity:
        items = [i for i in items if i["severity"] == severity]
    if scenario:
        items = [i for i in items if scenario.lower() in i["scenario"].lower()]
    if source_ip:
        items = [i for i in items if i["source_ip"] == source_ip]
    if since:
        items = [i for i in items if i["at"] >= since]
    items.sort(key=lambda i: i["at"], reverse=True)
    limit = max(1, min(int(limit), 200))
    try:
        off = history_store.decode_cursor(cursor)
    except ValueError:
        raise _err(400, "invalid_cursor", "invalid cursor") from None
    page = items[off: off + limit]
    return {"items": page, "next_cursor": history_store.encode_cursor(off + limit) if len(items) > off + limit else None, "status": status and {k: v for k, v in status.items() if k != "at"}}


@router.get("/decisions")
def decisions(limit: int = 100, cursor: str = "", p: Principal = Depends(viewer)):
    rows, status = core.read_decisions()
    items = [{"id": r.get("id"), "ip": r.get("value", ""), "scope": r.get("scope", "Ip"), "scenario": r.get("scenario", ""), "origin": r.get("origin", ""),
              "type": r.get("type", "ban"), "remaining": (r.get("duration") or "").split(".")[0]} for r in rows]
    limit = max(1, min(int(limit), 500))
    try:
        off = history_store.decode_cursor(cursor)
    except ValueError:
        raise _err(400, "invalid_cursor", "invalid cursor") from None
    return {"items": items[off: off + limit], "next_cursor": history_store.encode_cursor(off + limit) if len(items) > off + limit else None,
            "status": {k: v for k, v in status.items() if k != "at"}}


# ── cases and evidence ───────────────────────────────────────────────────────────────────────────────────────

@router.get("/cases")
def cases(status: str = "", severity: str = "", q: str = "", limit: int = 50, cursor: str = "", p: Principal = Depends(viewer)):
    _sync()
    try:
        return _hs().list_cases(status=status, severity=severity, q=q[:80], limit=limit, cursor=cursor or None)
    except ValueError:
        raise _err(400, "invalid_cursor", "invalid cursor") from None


@router.get("/cases/{case_id}")
def case(case_id: str, p: Principal = Depends(viewer)):
    _sync()
    c = _hs().get_case(case_id)
    if c is None:
        raise _err(404, "not_found", f"unknown case '{case_id}'")
    detail = live.case_detail(case_id)
    c["replay"] = detail["replay"] if detail else {"deterministic": None, "detail": "the alert is no longer held by CrowdSec; the stored evidence remains"}
    c["active"] = detail is not None
    return c


@router.get("/cases/{case_id}/evidence/{artifact_id}")
def evidence(case_id: str, artifact_id: str, p: Principal = Depends(viewer)):
    hs = _hs()
    if not hs.case_has_evidence(case_id, artifact_id):
        raise _err(404, "not_found", "no such evidence on this case")
    row = hs.get_evidence(artifact_id)
    if row is None or hashlib.sha256(row["content"].encode()).hexdigest() != row["sha256"]:
        raise _err(500, "integrity_error", "stored evidence does not match its digest")
    return Response(content=row["content"], media_type="application/json",
                    headers={"X-Content-SHA256": row["sha256"], "Content-Disposition": f'attachment; filename="{artifact_id}.json"'})


@router.post("/cases", status_code=201)
async def open_case(request: Request, p: Principal = Depends(operator), _rl=Depends(ratelimit.rate_limit)):
    """Stage a manual ban or unban for an address. It only proposes: a person still has to approve and execute it."""
    body = _body(await _json(request))
    kind = {"manual_block": "block", "manual_unblock": "unblock"}.get(str(body.get("type", "")))
    if kind is None:
        raise _err(422, "invalid_type", "type must be manual_block or manual_unblock")
    actor = p.actor if p.kind == "user" else "service"
    try:
        tok = ipguard.OPERATOR_IP.set(ratelimit.client_ip(request))
        try:
            return live.stage_manual(kind, str(body.get("ip", "")), reason=str(body.get("reason", "")), duration=str(body.get("duration", "")) or "", actor=actor, via=p.via)
        finally:
            ipguard.OPERATOR_IP.reset(tok)
    except ValueError as e:
        raise _err(422, "refused", str(e)) from None
    except live.ExecutionError as e:
        raise _err(409, e.code, str(e)) from None


# ── actions: queue, decision, execution, verification ────────────────────────────────────────────────────────

@router.get("/actions")
def actions(state: str = "", limit: int = 50, cursor: str = "", p: Principal = Depends(viewer)):
    _sync()
    try:
        return _hs().list_actions(state=state, limit=limit, cursor=cursor or None)
    except ValueError:
        raise _err(400, "invalid_cursor", "invalid cursor") from None


@router.get("/actions/{request_id}")
def action(request_id: str, p: Principal = Depends(viewer)):
    a = _hs().get_action(request_id)
    if a is None:
        raise _err(404, "not_found", f"unknown action '{request_id}'")
    a["mode"] = {"dry_run": not auth.block_enabled() or fixture_mode.status()["enabled"], "demo": fixture_mode.status()["enabled"]}
    return a


@router.get("/actions/{request_id}/verification")
def verification(request_id: str, p: Principal = Depends(viewer)):
    a = _hs().get_action(request_id)
    if a is None:
        raise _err(404, "not_found", f"unknown action '{request_id}'")
    return {"request_id": request_id, "verification": a["verification"], "receipt": a["receipt"]}


def _strip_approval(obj):
    """Drop every caller-supplied `_approval*` / `__approval*` key (any case), recursively: only the runtime and verified tokens create approvals."""
    if isinstance(obj, dict):
        return {k: _strip_approval(v) for k, v in obj.items() if not (isinstance(k, str) and k.lstrip("_").lower().startswith("approval"))}
    if isinstance(obj, list):
        return [_strip_approval(v) for v in obj]
    return obj


async def _json(request: Request):
    try:
        return _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        return {}


def _live_errors(fn):
    try:
        return fn()
    except KeyError:
        raise _err(404, "not_found", "unknown case or action") from None
    except live.StaleApproval as e:
        raise _err(409, "stale_approval", str(e)) from None
    except live.ExecutionError as e:
        raise _err(409, e.code, str(e)) from None


def _ensure_active(case_id: str) -> None:
    if case_id not in live.store().cases:
        if _hs().one("SELECT 1 AS x FROM cases WHERE case_id=?", (case_id,)):
            raise _err(409, "case_not_active", "this case is no longer held by CrowdSec: it can be read but not decided or executed")
        raise _err(404, "not_found", f"unknown case '{case_id}'")


@router.post("/cases/{case_id}/actions/{request_id}/decision")
async def decision(case_id: str, request_id: str, request: Request, _rl=Depends(ratelimit.rate_limit)):
    current(request)                                   # authenticate before looking at the body
    body = _body(await _json(request))
    if not isinstance(body.get("approved"), bool):
        raise _err(422, "invalid_body", "approved must be true or false")
    digest = body.get("digest")
    if not isinstance(digest, str) or len(digest) != 64:
        raise _err(422, "digest_required", "digest is required: the approval is bound to the exact action it was shown")
    p = human(request, "sentinel.decide", {"case_id": case_id, "request_id": request_id, "approved": body["approved"], "digest": digest})
    _ensure_active(case_id)
    out = _live_errors(lambda: live.decide(case_id, request_id, approved=body["approved"], actor=p.actor, via=p.via, role=p.role,
                                           external_actor=p.external_actor, digest=digest, rationale=str(body.get("rationale", ""))))
    a = _hs().get_action(request_id)
    return {"decision": a["decision"] if a else out["decision"], "action_state": a["state"] if a else "", "awaiting_execution": out["awaiting_execution"],
            "case_status": out["case_status"]}


@router.post("/cases/{case_id}/actions/{request_id}/execute")
async def execute(case_id: str, request_id: str, request: Request, _rl=Depends(ratelimit.rate_limit)):
    current(request)
    body = _body(await _json(request))
    digest = body.get("digest")
    if not isinstance(digest, str) or len(digest) != 64:
        raise _err(422, "digest_required", "digest is required: the execution is bound to the exact action that was approved")
    p = human(request, "sentinel.execute", {"case_id": case_id, "request_id": request_id, "digest": digest})
    _ensure_active(case_id)
    tok = ipguard.OPERATOR_IP.set(ratelimit.client_ip(request))        # never ban the operator's own address
    try:
        out = _live_errors(lambda: live.execute(case_id, request_id, actor=p.actor, via=p.via, role=p.role, digest=digest))
    finally:
        ipguard.OPERATOR_IP.reset(tok)
    return {"executed": out["executed"], "dry_run": out["dry_run"], "receipt": out["receipt"], "verification": out.get("verification_record"),
            "case_status": out["case_status"]}


# ── change feed and history ──────────────────────────────────────────────────────────────────────────────────

@router.get("/changes")
def changes(after: int = 0, limit: int = 100, p: Principal = Depends(viewer)):
    _sync()
    return _hs().changes(after, limit)


@router.get("/history")
def history(type: str = "", actor: str = "", limit: int = 50, cursor: str = "", p: Principal = Depends(viewer)):
    try:
        return _hs().history(type_=type, actor=actor, limit=limit, cursor=cursor or None)
    except ValueError:
        raise _err(400, "invalid_cursor", "invalid cursor") from None


# ── session ──────────────────────────────────────────────────────────────────────────────────────────────────

_LOGIN_LIMITER = ratelimit.TokenBucketLimiter()


@router.post("/session")
async def login(request: Request, response: Response):
    body = _body(await _json(request))
    name, password = str(body.get("username", ""))[:80], str(body.get("password", ""))[:1024]
    for key in (f"ip:{ratelimit.client_ip(request)}", f"user:{name}"):
        wait = _LOGIN_LIMITER.check(key)
        if wait:
            raise ApiError(429, "rate_limited", "too many sign-in attempts", headers={"Retry-After": str(int(wait) + 1)})
    ident = identity.get()
    if ident.count_users() == 0:
        identity.seed_from_environment()
    res = ident.login(name, password)
    if res is None:
        raise _err(401, "invalid_credentials", "wrong user name or password")
    principal, token = res
    secure = request.url.scheme == "https" or (ratelimit.trust_proxy() and request.headers.get("x-forwarded-proto") == "https")
    response.set_cookie(identity.COOKIE, token, httponly=True, samesite="strict", path="/", secure=secure, max_age=identity.ABSOLUTE_SECONDS)
    return {"user": principal.public(), "csrf": principal.csrf}


@router.get("/session")
def whoami(p: Principal = Depends(current)):
    return {"user": p.public(), "csrf": p.csrf if p.via == "session" else ""}


@router.delete("/session")
def logout(response: Response, p: Principal = Depends(current)):
    identity.get().logout(p.session_hash)
    response.delete_cookie(identity.COOKIE, path="/")
    return {"ok": True}


@router.post("/session/password")
async def change_password(request: Request, p: Principal = Depends(current)):
    body = _body(await _json(request))
    if p.kind != "user":
        raise _err(403, "forbidden", "only a person changes a password")
    ident = identity.get()
    if ident.check_password(p.name, str(body.get("current", ""))) is None:
        raise _err(403, "invalid_credentials", "the current password is wrong")
    try:
        ident.set_password(p.name, str(body.get("new", "")), actor=p.name, keep_session_hash=p.session_hash)
    except IdentityError as e:
        raise _err(422, "invalid_password", str(e)) from None
    return {"ok": True}


# ── users and actor links (admin) ────────────────────────────────────────────────────────────────────────────

@router.get("/users")
def users(p: Principal = Depends(admin)):
    return {"items": identity.get().list_users()}


@router.post("/users", status_code=201)
async def create_user(request: Request, p: Principal = Depends(admin)):
    b = _body(await _json(request))
    try:
        return identity.get().create_user(str(b.get("name", "")), str(b.get("password", "")), str(b.get("role", "")), display_name=str(b.get("display_name", "")), created_by=p.name)
    except IdentityError as e:
        raise _err(422 if "exists" not in str(e) else 409, "invalid_user", str(e)) from None


@router.patch("/users/{name}")
async def update_user(name: str, request: Request, p: Principal = Depends(admin)):
    b = _body(await _json(request))
    try:
        return identity.get().update_user(name, role=b.get("role"), disabled=b.get("disabled"), display_name=b.get("display_name"), actor=p.name)
    except IdentityError as e:
        raise _err(404 if "no such" in str(e) else 422, "invalid_user", str(e)) from None


@router.post("/users/{name}/password")
async def reset_password(name: str, request: Request, p: Principal = Depends(admin)):
    b = _body(await _json(request))
    try:
        identity.get().set_password(name, str(b.get("new", "")), actor=p.name)
    except IdentityError as e:
        raise _err(404 if "no such" in str(e) else 422, "invalid_password", str(e)) from None
    return {"ok": True}


@router.get("/actor-links")
def actor_links(p: Principal = Depends(admin)):
    return {"items": identity.get().list_links()}


@router.post("/actor-links", status_code=201)
async def link_actor(request: Request, p: Principal = Depends(admin)):
    b = _body(await _json(request))
    try:
        return identity.get().link_actor(str(b.get("external_actor", "")), str(b.get("user", "")), created_by=p.name)
    except IdentityError as e:
        raise _err(422, "invalid_link", str(e)) from None


@router.delete("/actor-links/{external_actor}")
def unlink_actor(external_actor: str, p: Principal = Depends(admin)):
    if not identity.get().unlink_actor(external_actor, actor=p.name):
        raise _err(404, "not_found", "no such link")
    return {"ok": True}
