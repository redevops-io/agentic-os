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
ENV_APPROVER_TOKENS = "AGENTIC_OS_APPROVER_TOKENS"  # "acme=<token>" -> distinct approver credential per tenant (optional)
ENV_TENANT_LIMITS = "AGENTIC_OS_TENANT_LIMITS"    # JSON {"acme": {"policy_refs": ["crm:read"], "budget_usd": 25}}
ENV_OPERATOR_ANON = "AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS"  # "1" = operator /invoke open when NO token is set (dev only)
ENV_GATE_READS = "AGENTIC_OS_GATE_READS"          # default ON; "0"/"false"/"off" opts OUT (unsafe off loopback)


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


class ConfigError(ValueError):
    """Misconfigured token env (duplicate / colliding tokens). Fail closed rather than make a token unreachable."""


@dataclass(frozen=True)
class Caller:
    """Who a valid token represents. ``tenant is None`` = the unscoped admin token (all tenants);
    otherwise the caller may only see/act on missions whose ``tenant`` equals this value.
    ``role``: "admin" | "requester" (tenant token) | "approver" (AGENTIC_OS_APPROVER_TOKENS)."""
    tenant: "str | None" = None
    role: str = "admin"


def _parse_pairs(env: str) -> "dict[str, str]":
    out: dict[str, str] = {}
    for part in os.environ.get(env, "").split(","):
        tenant, sep, tok = part.strip().partition("=")
        tenant, tok = tenant.strip(), tok.strip()
        if sep and tenant and tok:
            if tenant in out:
                raise ConfigError(f"{env}: tenant {tenant!r} listed twice")
            out[tenant] = tok
    return out


def tenant_tokens() -> "dict[str, str]":
    """Parse ``AGENTIC_OS_TENANT_TOKENS`` (``tenant=token`` pairs; malformed entries are ignored). Raises ConfigError
    if a token is shared by two tenants or equals the admin token (it would silently widen or shadow scope)."""
    out = _parse_pairs(ENV_TENANT_TOKENS)
    admin = configured_token()
    seen: dict[str, str] = {}
    for tenant, tok in out.items():
        if tok == admin:
            raise ConfigError(f"{ENV_TENANT_TOKENS}: tenant {tenant!r} token equals {ENV_TOKEN}")
        if tok in seen:
            raise ConfigError(f"{ENV_TENANT_TOKENS}: tenants {seen[tok]!r} and {tenant!r} share the same token")
        seen[tok] = tenant
    return out


def approver_tokens() -> "dict[str, str]":
    """Parse ``AGENTIC_OS_APPROVER_TOKENS`` (same ``tenant=token`` shape). Approver tokens must be distinct from the
    admin and tenant tokens and from each other."""
    out = _parse_pairs(ENV_APPROVER_TOKENS)
    taken = {configured_token(), *tenant_tokens().values()} - {""}
    seen: set[str] = set()
    for tenant, tok in out.items():
        if tok in taken or tok in seen:
            raise ConfigError(f"{ENV_APPROVER_TOKENS}: tenant {tenant!r} token is not distinct from the other tokens")
        seen.add(tok)
    return out


def tenant_limits(tenant: str) -> "tuple[list[str], float | None]":
    """Server-side ceiling for a tenant caller: (allowed grants, max budget USD or None = default cap).

    Read from ``AGENTIC_OS_TENANT_LIMITS``. A tenant with no entry gets NO grants (fail closed), and a
    wildcard grant in the config is ignored: grants are never a wildcard for a tenant caller.
    """
    import json  # noqa: PLC0415
    import math  # noqa: PLC0415
    raw = os.environ.get(ENV_TENANT_LIMITS, "").strip()
    if not raw:
        return [], None
    try:
        cfg = json.loads(raw)
    except ValueError as e:
        raise ConfigError(f"{ENV_TENANT_LIMITS}: not valid JSON") from e
    if not isinstance(cfg, dict):
        raise ConfigError(f"{ENV_TENANT_LIMITS}: expected an object keyed by tenant")
    entry = cfg.get(tenant)
    if not isinstance(entry, dict):
        return [], None
    refs = [r for r in (entry.get("policy_refs") or []) if isinstance(r, str) and r and r != "*"]
    cap = entry.get("budget_usd")
    if cap is not None:
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap < 0:
            raise ConfigError(f"{ENV_TENANT_LIMITS}: budget_usd for {tenant!r} must be a finite number >= 0")
        cap = float(cap)
    return refs, cap


def validate_config() -> None:
    """Raise ConfigError on a bad token env. Called when the routers are mounted so a bad config fails at startup."""
    tenant_tokens()
    approver_tokens()
    for t in tenant_tokens():
        tenant_limits(t)


def any_token_configured() -> bool:
    try:
        return bool(configured_token() or tenant_tokens() or approver_tokens())
    except ConfigError:
        return True      # misconfigured counts as configured: authenticate() will answer 503 with the reason


def reads_gated() -> bool:
    """Reads are gated BY DEFAULT. Only an explicit 0/false/no/off opts out (local demos on loopback only)."""
    return os.environ.get(ENV_GATE_READS, "").strip().lower() not in ("0", "false", "no", "off")


def resolve_caller(authorization: "str | None") -> Caller:
    """Authenticate ``Authorization: Bearer`` against the admin, tenant and approver tokens (constant-time, no early
    exit). 503 when no token is configured (or the config is invalid); 401 when missing/wrong."""
    from fastapi import HTTPException  # noqa: PLC0415
    try:
        admin, tenants, approvers = configured_token(), tenant_tokens(), approver_tokens()
    except ConfigError as e:
        raise HTTPException(503, f"endpoints disabled: invalid token configuration ({e})")
    if not admin and not tenants and not approvers:
        raise HTTPException(503, f"endpoints are disabled: {ENV_TOKEN} (or {ENV_TENANT_TOKENS}) is not set. "
                                 "Set a long random secret and send 'Authorization: Bearer <token>'.")
    presented = bearer_of(authorization)
    found: "Caller | None" = Caller(None, "admin") if token_matches(presented, admin) else None
    for tenant, tok in tenants.items():
        if token_matches(presented, tok) and found is None:
            found = Caller(tenant, "requester")
    for tenant, tok in approvers.items():
        if token_matches(presented, tok) and found is None:
            found = Caller(tenant, "approver")
    if found is None:
        raise HTTPException(401, "invalid or missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    return found


def authorize_operator_invoke(authorization: "str | None") -> Caller:
    """Gate for an operator's ``POST /invoke``. Same credentials as the other control routes (admin or tenant bearer
    token); an approver-only token is refused (403). With NO token configured it fails closed (503) unless
    ``AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS=1`` is set explicitly (local development only). Once any token is
    configured the flag is ignored and a valid bearer token is always required."""
    from fastapi import HTTPException  # noqa: PLC0415
    if not any_token_configured():
        if os.environ.get(ENV_OPERATOR_ANON, "").strip() == "1":
            return Caller(None, "admin")
        raise HTTPException(503, f"operator /invoke is disabled: {ENV_TOKEN} (or {ENV_TENANT_TOKENS}) is not set. "
                                 "Set a long random secret and send 'Authorization: Bearer <token>'.")
    caller = resolve_caller(authorization)
    if caller.role == "approver":
        raise HTTPException(403, "approver credentials cannot invoke capabilities")
    return caller
