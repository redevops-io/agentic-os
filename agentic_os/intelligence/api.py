"""FastAPI router for the Intelligence service — the plan's §3 API surface.

Thin: it delegates to an :class:`IntelligenceService`. Mount into the control plane with
`app.include_router(build_router(service))`. FastAPI is imported lazily so the kernel stays usable and
testable without the web dependency.

AUTHENTICATION. ``build_router(service)`` with no ``authenticator`` is the UNAUTHENTICATED DEMO: any caller is served
and the ``tenant`` in the request body is used as sent (it is a label, not an authorisation). The only place in this
repository that mounts it that way is ``demo_server.py`` over canned data. Do not mount it without an authenticator in
front of real data. ``build_router(service, authenticator=...)`` (see :mod:`agentic_os.intelligence.auth`) requires a
credential on every route and takes the tenant from the authenticated principal: a body that names another tenant is
refused (403) and a stored result of another tenant is not returned (404).

  POST /v1/intelligence/{domain}/{capability}   — resolve a DecisionNeed (domain is a label; routing is by
                                                  capability, which the service maps to the right family)
  POST /v1/intelligence/quote                   — estimate providers / cost / fields / latency before spend
  GET  /v1/intelligence/requests/{request_id}   — fetch a stored result by fingerprint (replay)
  GET  /v1/intelligence/capabilities            — the capabilities this service can serve
"""
# NB: no `from __future__ import annotations` — FastAPI must see the real BaseModel classes (defined inside
# build_router) to bind them as the request body, not resolve stringized annotations it cannot find.
import logging
from dataclasses import asdict
from typing import Optional

from runtime_contracts.protocol import Capability, DecisionNeed, IntelligenceResult

from .auth import IntelligenceAuthenticator
from .service import IntelligenceService

log = logging.getLogger("agentic_os.intelligence")


def _result_dict(res: IntelligenceResult) -> dict:
    return {**res.canonical_form(), "request_id": res.fingerprint()}


def build_router(service: IntelligenceService, *, authenticator: Optional[IntelligenceAuthenticator] = None):
    from fastapi import APIRouter, Depends, HTTPException, Header, Request
    from pydantic import BaseModel

    if authenticator is None:
        log.warning("intelligence router built WITHOUT an authenticator: unauthenticated demo mode, the request "
                    "body's tenant is used as sent. Pass authenticator=... before serving real data.")

    def _who(request: Request, authorization: Optional[str] = Header(default=None)) -> None:
        principal = authenticator.authenticate(authorization)
        if principal is None:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if not principal.tenant or principal.tenant != principal.tenant.strip():
            raise HTTPException(403, "the credential is not bound to a tenant")
        request.state.intelligence_principal = principal

    router = APIRouter(prefix="/v1/intelligence", tags=["intelligence"],
                       dependencies=[Depends(_who)] if authenticator is not None else [])

    def _tenant(request: Request, claimed: str) -> str:
        """The tenant of a call: the body's value in demo mode; the principal's when authenticated."""
        if authenticator is None:
            return claimed
        principal = getattr(request.state, "intelligence_principal", None)
        if principal is None:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if claimed and claimed != principal.tenant:
            raise HTTPException(403, "the request names a tenant other than the authenticated principal's")
        return principal.tenant

    class NeedBody(BaseModel):
        subject_refs: list[str] = []
        tenant: str = ""
        decision_case_id: str = ""
        objective: str = ""
        question: str = ""
        max_cost: float = 0.0
        min_confidence: float = 0.0
        freshness_limit_s: float = 0.0
        permitted_providers: list[str] = []
        prohibited_fields: list[str] = []
        as_of: str = ""
        known_at: str = ""

    class QuoteBody(NeedBody):
        capability: str

    def _need(capability: str, b: NeedBody, tenant: str) -> DecisionNeed:
        try:
            cap = Capability(capability)
        except ValueError:
            raise HTTPException(400, f"unknown capability '{capability}'")
        return DecisionNeed(
            decision_case_id=b.decision_case_id or "req", capability=cap,
            subject_refs=tuple(b.subject_refs), tenant=tenant, objective=b.objective, question=b.question,
            max_cost=b.max_cost, min_confidence=b.min_confidence, freshness_limit_s=b.freshness_limit_s,
            permitted_providers=tuple(b.permitted_providers), prohibited_fields=tuple(b.prohibited_fields),
            as_of=b.as_of, known_at=b.known_at)

    @router.get("/capabilities")
    def capabilities() -> dict:
        return {"capabilities": list(service.capabilities())}

    @router.post("/quote")
    def quote(request: Request, body: QuoteBody) -> dict:
        return asdict(service.quote(_need(body.capability, body, _tenant(request, body.tenant))))

    @router.get("/requests/{request_id}")
    def get_request(request: Request, request_id: str) -> dict:
        res = service.get(request_id)
        if res is None or (authenticator is not None and res.tenant != _tenant(request, "")):
            raise HTTPException(404, "request not found")
        return _result_dict(res)

    @router.post("/{domain}/{capability}")
    def resolve(request: Request, domain: str, capability: str, body: NeedBody) -> dict:
        return _result_dict(service.resolve(_need(capability, body, _tenant(request, body.tenant))))

    return router
