"""Server-side operator approval for the app's OWN HTTP routes (``/agent/run`` and friends).

The approval gate itself lives in ``core`` (a side-effecting core function stages a preview unless it is
handed an approval marker). This module is how a ROUTE obtains that marker legitimately: a request carrying
valid operator HTTP Basic credentials is treated as the operator's explicit approval and the route injects
``OPERATOR_MARKER`` server-side. Anything else -- no header, wrong credentials, no credential configured
(fail closed) -- gets ``None`` and therefore only a staged preview. A request BODY can never supply the
marker (routes strip every ``_approval*`` key first), and console/LLM tool calls never reach this module.

Credentials: ``OPERATOR_BASIC_AUTH`` or ``OUTREACH_BASIC_AUTH`` as ``user:pass`` (or the split
``OPERATOR_BASIC_AUTH_USER`` / ``_PASS``).
"""
from __future__ import annotations

import base64
import hmac
import os

OPERATOR_MARKER = {"approved": True, "by": "operator-basic"}


def operator_credential() -> tuple[str, str]:
    user = os.environ.get("OPERATOR_BASIC_AUTH_USER", "")
    pw = os.environ.get("OPERATOR_BASIC_AUTH_PASS", "")
    if not (user and pw):
        combo = os.environ.get("OPERATOR_BASIC_AUTH") or os.environ.get("OUTREACH_BASIC_AUTH") or ""
        user, _, pw = combo.partition(":")
    return user, pw


def is_operator(request) -> bool:
    user, pw = operator_credential()
    if not (user and pw):
        return False                      # fail closed: no credential configured => nobody is the operator
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        gu, _, gp = base64.b64decode(header[6:]).decode("utf-8", "replace").partition(":")
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(gu.encode(), user.encode()) & hmac.compare_digest(gp.encode(), pw.encode())


def operator_approval(request):
    """The marker the ROUTE injects into the core call, or ``None`` (staged preview only)."""
    if is_operator(request):
        return dict(OPERATOR_MARKER)
    from agentic_os.app_kit.route_authz import current_marker
    return current_marker()      # earned from an authenticated context by the route-authorization layer, never the body
