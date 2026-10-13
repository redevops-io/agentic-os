"""Registry of platform-managed secrets: how each is generated, validated and who consumes it.

Generation lives ONLY in init/bootstrap/entrypoints (never in a request path or an operator container: a
per-container random value would mismatch the runtime). Add a secret by appending a :class:`ManagedSecret`.
"""
from __future__ import annotations

import secrets as _s
from dataclasses import dataclass
from typing import Callable, Dict, Tuple

APPROVAL = "AGENTIC_OS_APPROVAL_SECRET"
TWENTY = "TWENTY_APP_SECRET"
OUTREACH = "OUTREACH_BASIC_AUTH"


@dataclass(frozen=True)
class ManagedSecret:
    name: str
    generate: Callable[[], str]
    validate: Callable[[str], Tuple[bool, str]]
    consumers: Tuple[str, ...]
    rotate_warning: str
    purpose: str = ""


def _hex_ok(v: str) -> Tuple[bool, str]:
    return (True, "") if len(v) >= 32 else (False, "shorter than 32 characters")


def _twenty_ok(v: str) -> Tuple[bool, str]:
    if "demo-only" in v:
        return False, "contains 'demo-only' (a published demo value)"
    return _hex_ok(v)


def _basic_ok(v: str) -> Tuple[bool, str]:
    user, sep, pw = v.partition(":")
    if not sep or not user:
        return False, "not in user:password format"
    if len(pw) < 20:
        return False, "password shorter than 20 characters"
    return True, ""


MANAGED: Dict[str, ManagedSecret] = {m.name: m for m in (
    ManagedSecret(
        APPROVAL, lambda: _s.token_hex(32), _hex_ok,
        ("mission runtime", "every HTTP operator container"),
        "Must be IDENTICAL on the mission runtime and ALL HTTP operators; restart them all after rotating. "
        "In-flight approval tokens become invalid and approved actions stay staged until re-approved.",
        "Signs/verifies approval tokens (#416)."),
    ManagedSecret(
        TWENTY, lambda: _s.token_hex(32), _twenty_ok,
        ("Twenty CRM server/worker",),
        "Rotating invalidates all Twenty sessions and API keys; users must log in again and the "
        "revenue agent's TWENTY_API_KEY must be recreated.",
        "Twenty JWT signing secret."),
    ManagedSecret(
        OUTREACH, lambda: "operator:" + _s.token_urlsafe(24), _basic_ok,
        ("revenue-agent", "console real actions (Basic-auth gate)"),
        "Anything calling the revenue-agent/console real actions with the old credential gets 401 until "
        "updated; restart the revenue-agent and console.",
        "Operator Basic-auth gate for real sends."),
)}

NAMES = tuple(MANAGED)
