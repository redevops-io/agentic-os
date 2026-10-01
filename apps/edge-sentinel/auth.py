"""Operator authentication for consequential actions — the AUTH gate that separates "approve in the demo"
(open) from "execute for real" (authenticated).

Copied from the live Growth/Partners pattern (agentic_os_enterprise `growth/app.py::_require_operator` /
`console/ops_app.py::_OpsAuth`): in-app HTTP Basic, FAIL-CLOSED (503 when no operator credential is
configured), 401 + WWW-Authenticate otherwise, constant-time compare. No Zitadel/OIDC/session/token — the
same mechanism intel.redevops.io and demo.redevops.io/partners use, so Edge Sentinel shares one operator
credential.

Credentials: SENTINEL_BASIC_AUTH_USER / SENTINEL_BASIC_AUTH_PASS, falling back to
PARTNERS_BASIC_AUTH_USER / PARTNERS_BASIC_AUTH_PASS (the shared ops credential).

A SECOND, independent flag — SENTINEL_BLOCK_ENABLED — gates whether an authenticated execution actually
mutates the edge; default is dry-run, so the public demo never touches the live core by accident (mirrors
PARTNERS_SEND_ENABLED).
"""
from __future__ import annotations

import base64
import binascii
import os
import secrets

from fastapi import HTTPException, Request

_REALM = 'Basic realm="ReDevOps Edge Sentinel Operator"'


def _operator_credential() -> tuple[str | None, str | None]:
    user = os.environ.get("SENTINEL_BASIC_AUTH_USER") or os.environ.get("PARTNERS_BASIC_AUTH_USER")
    pw = os.environ.get("SENTINEL_BASIC_AUTH_PASS") or os.environ.get("PARTNERS_BASIC_AUTH_PASS")
    return user, pw


def require_operator(request: Request) -> str:
    """FastAPI dependency. Returns the authenticated operator username, or raises:
    503 if no operator credential is configured (fail-closed), 401 if the Basic header is missing/invalid."""
    user, pw = _operator_credential()
    if not (user and pw):
        raise HTTPException(
            status_code=503,
            detail="operator approval is not configured (set SENTINEL_BASIC_AUTH_USER/_PASS)",
        )
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        raise HTTPException(status_code=401, detail="operator authentication required",
                            headers={"WWW-Authenticate": _REALM})
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=401, detail="malformed Basic credentials",
                            headers={"WWW-Authenticate": _REALM})
    got_user, _, got_pw = raw.partition(":")
    ok = secrets.compare_digest(got_user, user) and secrets.compare_digest(got_pw, pw)
    if not ok:
        raise HTTPException(status_code=401, detail="invalid operator credentials",
                            headers={"WWW-Authenticate": _REALM})
    return got_user


def block_enabled() -> bool:
    """Whether an AUTHENTICATED execution may actually mutate the edge. Default False (dry-run) so the public
    demo never bans/changes anything live by accident — a separate switch from authentication itself."""
    return os.environ.get("SENTINEL_BLOCK_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
