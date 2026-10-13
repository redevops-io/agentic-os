"""Bearer-token gate shared by the control surfaces that can mutate state (invoke / approve / deploy).

Policy (fail closed): the mutating endpoints require ``Authorization: Bearer <AGENTIC_OS_CONTROL_TOKEN>``
(constant-time compare). When the token is NOT configured they refuse to serve (503 with a clear message)
rather than run open. Read-only endpoints never call this and stay open.
"""
from __future__ import annotations

import hmac
import os

ENV_TOKEN = "AGENTIC_OS_CONTROL_TOKEN"


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
