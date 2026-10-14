"""Bearer-token gate shared by the control surfaces that can mutate state (invoke / approve / deploy).

Policy (fail closed): the mutating endpoints require ``Authorization: Bearer <AGENTIC_OS_CONTROL_TOKEN>``
(constant-time compare). When the token is NOT configured they refuse to serve (503 with a clear message)
rather than run open. Read-only endpoints never call this and stay open.
"""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

ENV_TOKEN = "AGENTIC_OS_CONTROL_TOKEN"
ENV_TENANT_TOKENS = "AGENTIC_OS_TENANT_TOKENS"   # "acme=<token>,globex=<token>" -> tenant-scoped callers
ENV_GATE_READS = "AGENTIC_OS_GATE_READS"          # "1" -> sensitive GETs (events/explain/plans/inbox/...) need a token too


def configured_token() -> str:
    return os.environ.get(ENV_TOKEN, "").strip()


def bearer_of(authorization: "str | None") -> str:
    if not authorization:
        return ""
    scheme, _, value = authorization.strip().partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def token_matches(presented: str, expected: str) -> bool:
    return bool(expected) and bool(presented) and hmac.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8"))


def require_control_token(authorization: "str | None") -> None:
    """Raise HTTPException 503 (token unset) or 401 (missing/wrong token). Returns None when authorised."""
    from fastapi import HTTPException  # noqa: PLC0415
    expected = configured_token()
    if not expected:
        raise HTTPException(503, f"mutating endpoints are disabled: {ENV_TOKEN} is not set. Set it to a long "
                                 "random secret and send 'Authorization: Bearer <token>'.")
    if not token_matches(bearer_of(authorization), expected):
        raise HTTPException(401, "invalid or missing bearer token", headers={"WWW-Authenticate": "Bearer"})


@dataclass(frozen=True)
class Caller:
    """Who a valid token represents. ``tenant is None`` = the unscoped admin token (all tenants);
    otherwise the caller may only see/act on missions whose ``tenant`` equals this value."""
    tenant: "str | None" = None


def tenant_tokens() -> "dict[str, str]":
    """Parse ``AGENTIC_OS_TENANT_TOKENS`` (``tenant=token`` pairs). Malformed / empty entries are ignored; a token
    equal to the admin token is ignored (it would silently widen scope)."""
    out: dict[str, str] = {}
    admin = configured_token()
    for part in os.environ.get(ENV_TENANT_TOKENS, "").split(","):
        tenant, sep, tok = part.strip().partition("=")
        tenant, tok = tenant.strip(), tok.strip()
        if sep and tenant and tok and tok != admin:
            out[tenant] = tok
    return out


def reads_gated() -> bool:
    return os.environ.get(ENV_GATE_READS, "").strip().lower() in ("1", "true", "yes", "on")


def resolve_caller(authorization: "str | None") -> Caller:
    """Authenticate ``Authorization: Bearer`` against the admin token and the tenant tokens (constant-time, no early
    exit). 503 when NO token of either kind is configured; 401 when missing/wrong. Returns the Caller."""
    from fastapi import HTTPException  # noqa: PLC0415
    admin, tenants = configured_token(), tenant_tokens()
    if not admin and not tenants:
        raise HTTPException(503, f"mutating endpoints are disabled: {ENV_TOKEN} (or {ENV_TENANT_TOKENS}) is not set. "
                                 "Set a long random secret and send 'Authorization: Bearer <token>'.")
    presented = bearer_of(authorization)
    found: "Caller | None" = Caller(None) if token_matches(presented, admin) else None
    for tenant, tok in tenants.items():
        if token_matches(presented, tok) and found is None:
            found = Caller(tenant)
    if found is None:
        raise HTTPException(401, "invalid or missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    return found
