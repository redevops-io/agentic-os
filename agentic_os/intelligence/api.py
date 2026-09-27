"""FastAPI router for the Intelligence service — the plan's §3 API surface.

Thin: it delegates to an :class:`IntelligenceService`. Mount into the control plane with
`app.include_router(build_router(service))`. FastAPI is imported lazily so the kernel stays usable and
testable without the web dependency.

  POST /v1/intelligence/{domain}/{capability}   — resolve a DecisionNeed (domain is a label; routing is by
                                                  capability, which the service maps to the right family)
  POST /v1/intelligence/quote                   — estimate providers / cost / fields / latency before spend
  GET  /v1/intelligence/requests/{request_id}   — fetch a stored result by fingerprint (replay)
  GET  /v1/intelligence/capabilities            — the capabilities this service can serve
"""
# NB: no `from __future__ import annotations` — FastAPI must see the real BaseModel classes (defined inside
# build_router) to bind them as the request body, not resolve stringized annotations it cannot find.
from dataclasses import asdict

from runtime_contracts.protocol import Capability, DecisionNeed, IntelligenceResult

from .service import IntelligenceService


def _result_dict(res: IntelligenceResult) -> dict:
    return {**res.canonical_form(), "request_id": res.fingerprint()}


def build_router(service: IntelligenceService):
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel

    router = APIRouter(prefix="/v1/intelligence", tags=["intelligence"])

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

    def _need(capability: str, b: NeedBody) -> DecisionNeed:
        try:
            cap = Capability(capability)
        except ValueError:
            raise HTTPException(400, f"unknown capability '{capability}'")
        return DecisionNeed(
            decision_case_id=b.decision_case_id or "req", capability=cap,
            subject_refs=tuple(b.subject_refs), tenant=b.tenant, objective=b.objective, question=b.question,
            max_cost=b.max_cost, min_confidence=b.min_confidence, freshness_limit_s=b.freshness_limit_s,
            permitted_providers=tuple(b.permitted_providers), prohibited_fields=tuple(b.prohibited_fields),
            as_of=b.as_of, known_at=b.known_at)

    @router.get("/capabilities")
    def capabilities() -> dict:
        return {"capabilities": list(service.capabilities())}

    @router.post("/quote")
    def quote(body: QuoteBody) -> dict:
        return asdict(service.quote(_need(body.capability, body)))

    @router.get("/requests/{request_id}")
    def get_request(request_id: str) -> dict:
        res = service.get(request_id)
        if res is None:
            raise HTTPException(404, "request not found")
        return _result_dict(res)

    @router.post("/{domain}/{capability}")
    def resolve(domain: str, capability: str, body: NeedBody) -> dict:
        return _result_dict(service.resolve(_need(capability, body)))

    return router
