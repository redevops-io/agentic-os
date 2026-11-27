"""Where a PERSON sees what agents proposed, and decides. No agent credential reaches these.

The person is who Projects can name from a credential the server validated (``products.principal``): the operator, the
desktop owner, a declared single owner, ``admin`` or ``approver:<tenant>``. A tenant credential sees and decides only its
own tenant, and decides only where the tenant is granted ``writes:decide``; a requester sees but never decides. A decision
names the digest of what was shown.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import APIRouter, Body, HTTPException

from agentic_os.app_kit.route_authz import RoutePolicy as P

from .preauthorization import PreauthorizationError
from .service import GovernedWrites, WriteError

#: a tenant credential needs this among the tenant's ``policy_refs``; the operator and ``admin`` hold everything
DECIDE_PERMISSION = "writes:decide"
WRITE_ROUTE_POLICIES = {
    "POST /api/writes/{request_id}/decision": P(
        "writes.decide", "human_only", permission=DECIDE_PERMISSION,
        rationale="approving or rejecting a change an agent proposed is a person's decision"),
    "POST /api/writes/preauthorizations": P(
        "writes.preauthorize", "human_only", permission=DECIDE_PERMISSION,
        rationale="preauthorizing an agent's changes is a person's decision, made in advance"),
    "POST /api/writes/preauthorizations/{preauthorization_id}/revoke": P(
        "writes.preauthorize_revoke", "human_only", permission=DECIDE_PERMISSION,
        rationale="ending a preauthorization is a person's decision"),
}
_STATUS = {"not_found": 404, "invalid": 422, "conflict": 409, "unavailable": 503, "refused": 403, "too_many": 429, "denied": 403}
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{4,80}$")


def _http(e: WriteError) -> HTTPException:
    return HTTPException(_STATUS.get(e.code, 400), {"error": e.code, "detail": e.detail})


def make_router(writes: GovernedWrites, principal: Callable[[], Optional[str]]) -> APIRouter:
    r = APIRouter()

    def require_person() -> Tuple[str, Optional[str], bool]:
        """(who, the tenant they are limited to or None, whether they may decide)"""
        who = principal()
        if not who:
            raise HTTPException(401, {"error": "principal_required", "detail": "Sign in to Projects to see and decide proposed changes."})
        role, _, tenant = who.partition(":")
        if role in ("approver", "requester") and tenant:
            return who, tenant, role == "approver"
        return who, None, True

    def checked(request_id: str) -> str:
        if not _ID.match(request_id):
            raise HTTPException(404, {"error": "not_found", "detail": "no such request"})
        return request_id

    @r.get("/api/writes")
    def _list(state: str = "", limit: int = 50) -> Dict[str, Any]:
        _, tenant, decides = require_person()
        return {"requests": writes.list(tenant=tenant, state=state, limit=limit, for_approver=decides)}

    # (declared before /api/writes/{request_id}, which would otherwise take "preauthorizations" for a request id)
    def person_who_decides() -> Tuple[str, Optional[str]]:
        who, tenant, decides = require_person()
        if not decides:
            raise HTTPException(403, {"error": "refused", "detail": "this needs an approver credential"})
        return who, tenant

    @r.get("/api/writes/preauthorizations")
    def _preauthorizations(limit: int = 50) -> Dict[str, Any]:
        _, tenant = person_who_decides()
        return {"preauthorizations": writes.preauthorizations.list(tenant=tenant, limit=max(1, min(limit, 200)))}

    @r.post("/api/writes/preauthorizations")
    def _preauthorize(body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        who, tenant = person_who_decides()
        allowed = {"tenant", "agent", "actions", "match", "max_uses", "minutes", "reason", "budget"}
        if set(body) - allowed or not isinstance(body.get("actions"), list) or not isinstance(body.get("agent"), str):
            raise HTTPException(422, {"error": "invalid", "detail": "body: {agent, actions[], max_uses, minutes, reason[, match, tenant]}"})
        named = str(body.get("tenant") or tenant or "")
        if tenant is not None and named != tenant:            # a tenant's approver grants for that tenant only
            raise HTTPException(403, {"error": "refused", "detail": "you can only preauthorize changes of your own tenant"})
        try:
            return writes.preauthorize(tenant=named, requester=body["agent"], actions=body["actions"], match=body.get("match"),
                                       budget=body.get("budget"),
                                       max_uses=body.get("max_uses"), minutes=body.get("minutes"), created_by=who,
                                       reason=body.get("reason"))
        except PreauthorizationError as e:
            raise HTTPException(_STATUS.get(e.code, 403 if e.code == "disabled" else 400), {"error": e.code, "detail": e.detail})

    @r.post("/api/writes/preauthorizations/{preauthorization_id}/revoke")
    def _revoke(preauthorization_id: str) -> Dict[str, Any]:
        who, tenant = person_who_decides()
        try:
            return writes.preauthorizations.revoke(preauthorization_id, by=who, tenant=tenant)
        except PreauthorizationError as e:
            raise HTTPException(_STATUS.get(e.code, 400), {"error": e.code, "detail": e.detail})

    @r.get("/api/writes/{request_id}")
    def _get(request_id: str) -> Dict[str, Any]:
        _, tenant, decides = require_person()
        try:
            return writes.get(checked(request_id), tenant=tenant, for_approver=decides)
        except WriteError as e:
            raise _http(e)

    @r.post("/api/writes/{request_id}/decision")
    def _decide(request_id: str, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        who, tenant, decides = require_person()
        if not decides:
            raise HTTPException(403, {"error": "refused", "detail": "deciding needs an approver credential"})
        approved, digest = body.get("approved"), body.get("digest")
        attested = body.get("attested", False)
        if (set(body) - {"approved", "digest", "attested"} or not isinstance(approved, bool) or not isinstance(attested, bool)
                or not isinstance(digest, str) or not _DIGEST.match(digest)):
            raise HTTPException(422, {"error": "invalid", "detail": "body must be {approved: bool, digest: <64 hex>[, attested: bool]}"})
        try:
            return writes.decide(checked(request_id), approve=approved, digest=digest, approver=who, tenant=tenant, attested=attested)
        except WriteError as e:
            raise _http(e)

    return r


__all__ = ["make_router", "WRITE_ROUTE_POLICIES", "DECIDE_PERMISSION"]
