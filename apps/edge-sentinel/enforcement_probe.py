"""An opt-in, read-only page that says whether the edge holds a ban for ONE documentation address.

Why it exists: a quality check (redevops-qa) can only verify a remediation if it can observe the real system afterwards. This page is
that observation for a ban: `GET /enforcement/<address>` reads the CrowdSec decisions for the address (the same read-back the ban itself
uses) and prints one word in an element a browser test can find.

Limits, on purpose:
  * off unless SENTINEL_ENFORCEMENT_PROBE=1, and never on with the fixture core (EDGE_SENTINEL_DEMO): there is no edge to read then;
  * only documentation addresses (RFC 5737, RFC 3849) can be asked about, so the page never says anything about a real address;
  * it reads and never writes; it needs no sign-in, which is why `/health` reports whether it is on.
"""
from __future__ import annotations

import html
import ipaddress
import os

import httpx

from . import core, fixture_mode

DOCUMENTATION_NETS = tuple(ipaddress.ip_network(c) for c in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32"))
PATH_PREFIX = "/enforcement/"
BLOCKED, NOT_BLOCKED, UNKNOWN = "blocked", "not blocked", "unknown"


def enabled(env=None) -> bool:
    e = os.environ if env is None else env
    on = str(e.get("SENTINEL_ENFORCEMENT_PROBE", "")).strip().lower() in ("1", "true", "yes", "on")
    return on and not fixture_mode.status()["enabled"]


def status() -> dict:
    return {"enabled": enabled(), "scope": "documentation addresses only (RFC 5737, RFC 3849)", "path": PATH_PREFIX + "<address>"}


def documentation_address(raw: str) -> "str | None":
    """The canonical text of a single documentation address, or None for anything else (a real address, a range, junk)."""
    try:
        ip = ipaddress.ip_address((raw or "").strip())
    except ValueError:
        return None
    return str(ip) if any(ip.version == n.version and ip in n for n in DOCUMENTATION_NETS) else None


def state(ip: str) -> str:
    """`blocked` only when the read-back finds an active ban for exactly this address; `unknown` when the edge could not be read."""
    try:
        with httpx.Client(timeout=5.0) as client:
            active, seen = core._ban_active(client, ip, "Ip")
    except Exception:  # noqa: BLE001 - a failed read is reported as unknown, never as "not blocked"
        return UNKNOWN
    if seen is None:
        return UNKNOWN
    return BLOCKED if active else NOT_BLOCKED


def page(ip: str, st: str) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Edge enforcement</title></head><body><main>"
            "<h1>Edge enforcement</h1>"
            f"<p>Address <code data-testid=\"enforcement-address\">{html.escape(ip)}</code> is "
            f"<strong data-testid=\"enforcement-state\">{html.escape(st)}</strong> at the edge.</p>"
            "<p>Read from the CrowdSec decisions at the time of this request. Documentation addresses only.</p>"
            "</main></body></html>")
