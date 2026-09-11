"""Phase 7 — a REST delegation adapter (plan §7 Phase 7).

A second protocol over the *same* governed path — it adds no authority, exactly like the MCP
adapter. Transport-agnostic: a REST transport (FastAPI/ASGI) hands it the Authorization header and
the parsed request; it verifies the bearer and forwards to `AgentGateway`:

    GET  /capabilities                     → list_capabilities(authorization)   (the filtered set)
    POST /capabilities/{name}:invoke       → invoke(authorization, name, body)  (one governed call)

Only after MCP usage is validated (plan §7) — additional adapters (event/webhook triggers, agent
federation) follow the same shape.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..auth import GatewayAuthError, TokenVerifier, bearer_token
from ..contracts import GatewayRequest
from ..gateway import AgentGateway


class RestGatewayAdapter:
    """REST view of the gateway. Same auth + governance as the MCP bridge; different envelope."""

    def __init__(self, gateway: AgentGateway, verifier: TokenVerifier) -> None:
        self.gateway = gateway
        self.verifier = verifier

    def _principal(self, authorization: Optional[str]):
        gp = self.verifier.verify(bearer_token(authorization) or "")
        if gp is None:
            raise GatewayAuthError("invalid or missing token")   # transport → 401
        return gp

    def list_capabilities(self, authorization: Optional[str]) -> Dict[str, Any]:
        gp = self._principal(authorization)
        return {"capabilities": self.gateway.capabilities_for(gp)}

    def invoke(self, authorization: Optional[str], capability: str,
               arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        gp = self._principal(authorization)
        arguments = arguments or {}
        result = self.gateway.invoke(
            GatewayRequest(gp, capability, arguments, protocol="rest",
                           idempotency_key=arguments.get("idempotency_key")))
        return result.client_view()


def build_fastapi_router(adapter: RestGatewayAdapter):
    """Optional: a FastAPI APIRouter mounting the two endpoints onto `adapter`. Imported lazily so
    the core package needs no web framework at import time; the transport supplies the request's
    Authorization header to the adapter methods above."""
    from fastapi import APIRouter, Header, HTTPException, Request  # lazy

    router = APIRouter()

    @router.get("/capabilities")
    def _caps(authorization: str = Header(default="")):
        try:
            return adapter.list_capabilities(authorization)
        except GatewayAuthError:
            raise HTTPException(status_code=401, detail="unauthorized")

    @router.post("/capabilities/{name}:invoke")
    async def _invoke(name: str, request: Request, authorization: str = Header(default="")):
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return adapter.invoke(authorization, name, body if isinstance(body, dict) else {})
        except GatewayAuthError:
            raise HTTPException(status_code=401, detail="unauthorized")

    return router
