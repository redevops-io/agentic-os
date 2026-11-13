"""Operator authentication for consequential actions — the AUTH gate that separates "approve in the demo"
(open) from "execute for real" (authenticated).

In-app HTTP Basic, FAIL-CLOSED (503 when no operator credential is configured), 401 + WWW-Authenticate
otherwise, constant-time compare. No OIDC, session or token: one operator credential shared with the other
ReDevOps apps that use the PARTNERS_BASIC_AUTH_* pair.

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

from . import secret_files as _secret_files  # noqa: E402
_secret_files.load_file_secrets()

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


# ── standalone profile: require the operator credential on EVERY route ────────────────────────────────────────
# The reference behaviour above (reads open, execution authenticated) is what the rAIlsApps distribution relies on and
# it is unchanged when SENTINEL_REQUIRE_AUTH is unset. The standalone image and its Compose/Kubernetes files set
# SENTINEL_REQUIRE_AUTH=1: the dashboard, every API and the operator manifest then need the same HTTP-Basic
# credential, the app refuses to start without one (security.auth_required_guard), and only the two probe routes
# below stay public. `/invoke` is not covered by this gate on purpose: the operator router authenticates it itself
# (AGENTIC_OS_CONTROL_TOKEN, 503 when unset, plus execution authorization in the production profile).
PUBLIC_PATHS = frozenset({"/health", "/readyz"})
SELF_AUTHENTICATED_PATHS = frozenset({"/invoke"})


def require_auth_everywhere(env=None) -> bool:
    e = os.environ if env is None else env
    return str(e.get("SENTINEL_REQUIRE_AUTH", "")).strip().lower() in ("1", "true", "yes", "on")


def basic_header_valid(header: str) -> bool:
    """Constant-time check of an Authorization header against the configured operator credential."""
    user, pw = _operator_credential()
    if not (user and pw) or not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return False
    got_user, _, got_pw = raw.partition(":")
    ok_user = secrets.compare_digest(got_user.encode(), user.encode())
    ok_pw = secrets.compare_digest(got_pw.encode(), pw.encode())
    return ok_user and ok_pw


def align_kernel_operator_credential(env=None) -> None:
    """The kernel's route-authorization layer recognises a human by OPERATOR_BASIC_AUTH_USER/_PASS, a different pair
    from this app's. In the standalone profile one credential must serve both layers, so an unset kernel pair is filled
    from the app's pair (never overriding an explicit one). The values are only copied inside the process."""
    e = os.environ if env is None else env
    if not require_auth_everywhere(e):
        return
    user = e.get("SENTINEL_BASIC_AUTH_USER") or e.get("PARTNERS_BASIC_AUTH_USER")
    pw = e.get("SENTINEL_BASIC_AUTH_PASS") or e.get("PARTNERS_BASIC_AUTH_PASS")
    if user and pw and not (e.get("OPERATOR_BASIC_AUTH_USER") and e.get("OPERATOR_BASIC_AUTH_PASS")):
        e["OPERATOR_BASIC_AUTH_USER"], e["OPERATOR_BASIC_AUTH_PASS"] = user, pw
