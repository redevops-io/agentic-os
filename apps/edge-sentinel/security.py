"""Secure defaults (audit item P9): what the app does when nobody configured anything.

Three things were default-unsafe, all of them harmless on the operator's own machine and all of them
harmful on a shared network or a public host:

1. it bound `0.0.0.0` — a SOC dashboard with approval-gated actions, listening on every interface;
2. the Wazuh client passed `verify=False` unconditionally — the API is HTTPS with a self-signed
   certificate by default, so the code simply stopped checking certificates, for everyone;
3. every page fetched Roboto from `fonts.googleapis.com`/`fonts.gstatic.com` — an outbound request on
   every view, from a machine that may be air-gapped or monitored, and no content policy at all.

The defaults here are loopback-only, TLS-verified, no third-party request, and a content policy that
allows only this origin. Each one can be opened deliberately, by an explicit environment setting that
says what is being opened. Nothing here reads a network or touches the edge.
"""
from __future__ import annotations

import ipaddress
import os

# The dashboard is a single-operator tool: on loopback it is a local app for one user, so it is the only
# bind that needs no permission slip. Anything else is "exposed" and must be asked for by name.
DEFAULT_BIND_HOST = "127.0.0.1"
LOOPBACK_HOSTNAMES = frozenset({"localhost", "::1", "ip6-localhost", "ip6-loopback"})

# The page is rendered with inline <style> and one inline <script> (the case-decision buttons) and it
# talks only to its own origin, so 'self' + 'unsafe-inline' for style/script is what the app actually
# needs; everything else — objects, frames, base, forms, fonts, third-party anything — is refused.
# Dropping 'unsafe-inline' would mean serving the CSS/JS as files: a real improvement, not this one.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; object-src 'none'")

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",           # the dashboard must never be framed (click-jacking a ban button)
    "Referrer-Policy": "no-referrer",    # case ids and IP addresses must not leak to a referrer
}

_TRUTHY = ("1", "true", "yes", "on")


def _env(env):
    """An explicitly passed mapping wins even when it is empty — `env={}` means "check this mapping",
    not "I have nothing, use the process environment". (Passing `env or os.environ` silently did the
    latter, so a test that passed an empty mapping read the ambient environment instead.)"""
    return os.environ if env is None else env


def _flag(env, name: str) -> bool:
    return str(_env(env).get(name, "")).strip().lower() in _TRUTHY


def is_loopback(host: str) -> bool:
    """Loopback means reachable from this machine only, decided by parsing the address, never by prefix:
    `"0"` is `0.0.0.0` — every interface, the opposite of loopback — and a host that merely starts with
    "127." is somebody else's DNS name. True only for a real 127.0.0.0/8 or ::1 address, or localhost."""
    h = (host or "").strip().strip("[]")
    if not h:
        return False
    if h in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def resolve_bind_host(env=None) -> str:
    """The port is reachable-by-default-free: unset SENTINEL_BIND_HOST means loopback."""
    return str(_env(env).get("SENTINEL_BIND_HOST", DEFAULT_BIND_HOST)).strip() or DEFAULT_BIND_HOST


def operator_credential_configured(env=None) -> bool:
    """Same two credential pairs `auth.require_operator` accepts (dedicated, then the shared ops pair)."""
    e = _env(env)
    return bool((e.get("SENTINEL_BASIC_AUTH_USER") and e.get("SENTINEL_BASIC_AUTH_PASS"))
                or (e.get("PARTNERS_BASIC_AUTH_USER") and e.get("PARTNERS_BASIC_AUTH_PASS")))


class ExposureRefused(RuntimeError):
    """The requested bind is reachable and nobody asked for it, twice over. A normal application
    exception, so a test can assert on it and a process boundary can turn it into a startup message;
    raised from a server startup hook it fails that startup (uvicorn aborts), so nothing is served.
    Only the `python -m edge-sentinel.app` entry point converts it into a user-facing exit."""


def exposure_guard(host: str, env=None) -> None:
    """Refuse a reachable bind that nobody asked for. Raises ExposureRefused; the CLI boundary turns
    that into a startup error, and a uvicorn startup hook fails the boot with it.

    Loopback: always allowed. Non-loopback needs SENTINEL_ALLOW_EXPOSURE=1 (a deployment decision the
    image does NOT make for you), and on top of that either an operator credential or
    SENTINEL_ALLOW_UNAUTHENTICATED_EXPOSURE=1 — the last one is the public-demo case: a read-only dashboard
    where approving a ban is deliberately open and executing one stays authenticated and dry-run.
    """
    e = _env(env)
    if is_loopback(host):
        return
    if not _flag(e, "SENTINEL_ALLOW_EXPOSURE"):
        raise ExposureRefused(
            f"refusing to listen on {host}: a reachable bind must be asked for. Bind 127.0.0.1 (the default) "
            "for local use, or set SENTINEL_ALLOW_EXPOSURE=1 deliberately — in a container, where exposure is "
            "the point, set it on the run/compose command, not in the image.")
    if not operator_credential_configured(e) and not _flag(e, "SENTINEL_ALLOW_UNAUTHENTICATED_EXPOSURE"):
        raise ExposureRefused(
            f"refusing to listen on {host} with no operator credential configured: anyone who can reach the "
            "port could approve actions and read every case. Set SENTINEL_BASIC_AUTH_USER/_PASS, or "
            "SENTINEL_ALLOW_UNAUTHENTICATED_EXPOSURE=1 for a deliberately open read-only demo (execution "
            "stays authenticated and dry-run by default).")


def tls_verify(name: str, env=None) -> bool:
    """Certificate verification is ON unless the operator turns it off by name, e.g. WAZUH_TLS_VERIFY=off.
    An empty value counts as unset (verified) — an accidental empty env var must not disable checking."""
    raw = str(_env(env).get(name, "1")).strip().lower()
    if raw == "":
        return True
    return raw not in ("0", "false", "no", "off")


def tls_verify_or_ca(name: str, env=None):
    """What to hand a TLS client: True (system CAs, the default), False (deliberate opt-out), or the CA
    bundle path the operator named — Wazuh ships a self-signed certificate, and pointing at the CA that
    signed it is the fix; turning verification off is not."""
    raw = str(_env(env).get(name, "1")).strip()
    low = raw.lower()
    if low in _TRUTHY or low == "":
        return True
    if low in ("0", "false", "no", "off"):
        return False
    return raw


def tls_verify_note(name: str, env=None) -> str:
    """Say it out loud in whatever the user reads, the same way sample data is labelled."""
    return "" if tls_verify(name, env) else f" (TLS certificate NOT verified — {name}=off)"
