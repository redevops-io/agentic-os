"""Request-body handling shared by the permissions routes (control plane and the public showcase).

* the body must be a JSON object (anything else is a clean 400, never an unhandled 500);
* a grant's ``actions`` takes its default ["read"] only when the key is ABSENT or null: an explicit empty list is passed through and
  rejected by the store (SC-06), never silently widened to read.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import MutableHeaders

_log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
SCOPE_FIELDS = ("actions", "row_scope", "row_column", "row_values", "masked_columns")


async def json_object(request: Request, *, limit: int = MAX_BODY_BYTES) -> dict:
    """The request's JSON body as a dict, or HTTP 400 (not JSON / not an object) or 413 (too large)."""
    raw = await request.body()
    if len(raw) > limit:
        raise HTTPException(413, f"request body too large (limit {limit} bytes)")
    try:
        import json
        body = json.loads(raw.decode("utf-8") or "null")
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "the request body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "the request body must be a JSON object")
    return body


def create_kwargs(b: dict) -> dict:
    """The keyword arguments for ``GrantStore.add`` / the overlay from a POST body (absent or null ``actions`` -> read)."""
    actions = b.get("actions")
    return dict(actions=["read"] if actions is None else actions, row_scope=b.get("row_scope", "all"),
                row_column=b.get("row_column", "owner"), row_values=b.get("row_values") or [],
                masked_columns=b.get("masked_columns") or [])


def update_fields(b: dict) -> dict:
    """The scope fields present in a PUT body (an explicit ``actions: []`` is kept, so the store can refuse it)."""
    return {k: b[k] for k in SCOPE_FIELDS if k in b}


# ───────────────────────── the ADMINISTRATIVE API contract (control plane and showcase admin routes) ─────────────────────────
# Every route under /api/permissions/* — grant list, status, preview, authorize (diagnostics), create, update, remove — requires an
# admin credential. FAIL CLOSED: no credential, a wrong, empty or malformed one, or no server-side configuration at all is NEVER a public
# route (401 / 503). The secret-derived check is constant-time. A credential in the query string is refused (400): it would end up in
# logs, proxies and browser history.

_QUERY_CREDENTIAL_NAMES = frozenset({"api_key", "apikey", "api-key", "x-api-key", "x_api_key", "key", "token", "access_token",
                                     "auth", "authorization", "password", "passwd", "secret", "bearer", "admin_key", "adminkey"})


def reject_query_credentials(request: Request) -> None:
    """HTTP 400 when the query string carries anything credential-like (checked on every admin route, before and regardless of the
    headers: a client that puts a key in a URL is told to stop, and the key is not honoured)."""
    bad = sorted(k for k in request.query_params if k.strip().lower() in _QUERY_CREDENTIAL_NAMES)
    if bad:
        raise HTTPException(400, "credentials must not be sent in the query string (use the X-API-Key or Authorization: Bearer header)")


def presented_credential(request: Request) -> str:
    """The credential the request carries: ``X-API-Key``, else ``Authorization: Bearer``. Anything else (Basic, a cookie, a query
    parameter) is not a credential for this API."""
    key = request.headers.get("x-api-key") or ""
    if key:
        return key
    auth = request.headers.get("authorization") or ""
    scheme, _, value = auth.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


ADMIN_KEY_ENV = "PERMISSIONS_ADMIN_KEY"
MIN_ADMIN_KEY_LENGTH = 24
_short_key_logged = False
_disabled_logged = False

# ───────────────────────── failed-attempt throttle (PP-04) ─────────────────────────
# Per client: after FAIL_LIMIT wrong credentials inside FAIL_WINDOW_SECONDS the client gets 429 (Retry-After) until the window ends, for good
# guesses too (no oracle while locked). A missing credential is not a guess and is not counted. Memory is bounded (MAX_TRACKED_CLIENTS).
# The client is the connection peer; X-Forwarded-For is believed ONLY when the peer is listed in PERMISSIONS_TRUSTED_PROXIES (addresses or
# CIDRs, comma separated), and then the rightmost address that is not itself a trusted proxy is the client. Behind a proxy that is not
# configured every client shares the proxy's address and so one lockout: set PERMISSIONS_TRUSTED_PROXIES. A lockout only closes the admin API
# for that client; it never opens anything and does not touch enforcement.
# RT-01: that hard lock is only right where the client identity is TRUSTWORTHY (PERMISSIONS_TRUSTED_PROXIES configured and the client is a distinct address). With
# no trusted-proxy setting every visitor behind the reverse proxy shares ONE bucket (the proxy's address), and a hard lock would let ten anonymous wrong keys lock
# the real admin out: there the bucket BACKS OFF instead (each request waits min(cap, base * 2^(failures - limit)) seconds, for correct, missing and wrong keys
# alike, so the slow-down is no oracle; the number of sleeping threads is bounded and the overflow is a uniform 429). IPv6 clients are bucketed by /64.
FAIL_LIMIT = 10
FAIL_WINDOW_SECONDS = 300.0
BACKOFF_BASE = 0.25
BACKOFF_CAP = 5.0
MAX_BACKOFF_WAITERS = 8
MAX_TRACKED_CLIENTS = 4096
_failures: dict = {}                      # client id -> [failure count, window start (monotonic)]
_failures_lock = threading.Lock()
_sleep = time.sleep
_waiters = 0
_proxy_warned = False


def _note_failure(client: str) -> None:
    now = time.monotonic()
    with _failures_lock:
        if len(_failures) >= MAX_TRACKED_CLIENTS // 2:
            for k in [k for k, (_, start) in _failures.items() if now - start >= FAIL_WINDOW_SECONDS]:
                del _failures[k]
        entry = _failures.get(client)
        if entry is None or now - entry[1] >= FAIL_WINDOW_SECONDS:
            if entry is None and len(_failures) >= MAX_TRACKED_CLIENTS:
                del _failures[min(_failures, key=lambda k: _failures[k][1])]          # evict the oldest window
            _failures[client] = [1, now]
        else:
            entry[0] += 1


def _locked_for(client: str) -> int:
    """Seconds this client stays locked out (0 = not locked)."""
    now = time.monotonic()
    with _failures_lock:
        entry = _failures.get(client)
        if entry is None:
            return 0
        if now - entry[1] >= FAIL_WINDOW_SECONDS:
            del _failures[client]
            return 0
        return max(1, int(FAIL_WINDOW_SECONDS - (now - entry[1]) + 0.999)) if entry[0] >= FAIL_LIMIT else 0


def _failure_count(client: str) -> int:
    now = time.monotonic()
    with _failures_lock:
        entry = _failures.get(client)
        return entry[0] if entry is not None and now - entry[1] < FAIL_WINDOW_SECONDS else 0


def _backoff_delay(failures: int) -> float:
    """Seconds a request from a shared bucket waits: nothing up to the limit, then doubling from ``BACKOFF_BASE``, capped."""
    if failures < FAIL_LIMIT:
        return 0.0
    return min(BACKOFF_CAP, BACKOFF_BASE * (2 ** (failures - FAIL_LIMIT)))


def _clear_failures(client: str) -> None:
    with _failures_lock:
        _failures.pop(client, None)


def _trusted_proxies() -> list:
    out = []
    for item in os.environ.get("PERMISSIONS_TRUSTED_PROXIES", "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            out.append(item)                  # a literal peer name (never matches an address)
    return out


def _is_trusted(addr: str, trusted: list) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr in trusted
    return any(isinstance(n, (ipaddress.IPv4Network, ipaddress.IPv6Network)) and ip.version == n.version and ip in n for n in trusted)


def _bucket(addr: str) -> str:
    """The throttle bucket of an address: IPv4 as is, an IPv4-mapped IPv6 address as its IPv4, any other IPv6 address as its /64 (one subscriber
    holds a whole /64, so per-address buckets would let one host rotate through 2^64 identities)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def client_identity(request) -> tuple:
    """``(bucket id, trustworthy)``. TRUSTWORTHY only when ``PERMISSIONS_TRUSTED_PROXIES`` is configured AND the client is a distinct address: either the
    forwarded client behind a trusted proxy, or a direct peer that is not itself a trusted proxy. Otherwise the peer is the proxy (or unknown) and the
    bucket is shared by everybody behind it."""
    peer = (getattr(request, "client", None) and request.client.host) or "unknown"
    trusted = _trusted_proxies()
    if not trusted:
        return _bucket(peer), False
    if not _is_trusted(peer, trusted):
        return _bucket(peer), True
    for hop in reversed([h.strip() for h in (request.headers.get("x-forwarded-for") or "").split(",") if h.strip()]):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return _bucket(peer), False         # a malformed hop: believe nothing
        if not _is_trusted(hop, trusted):
            return _bucket(hop), True
    return _bucket(peer), False


def client_id(request) -> str:
    """The throttle bucket this request counts against (see ``client_identity``)."""
    return client_identity(request)[0]


def warn_if_unprotected_throttle() -> None:
    """Once, at start-up: the admin API is reachable (a usable key is configured) but ``PERMISSIONS_TRUSTED_PROXIES`` is not set, so everything behind a
    reverse proxy shares one throttle bucket (failures slow the admin API down for everyone; it is never locked). Set it to the proxy's address."""
    global _proxy_warned
    if _proxy_warned or not admin_key_from_env() or _trusted_proxies():
        return
    _proxy_warned = True
    _log.warning("PERMISSIONS_TRUSTED_PROXIES is not set while the permissions admin API is enabled: every client behind a reverse proxy shares ONE throttle "
                 "bucket (the proxy's address), so failed attempts only SLOW the admin API down for everybody instead of locking out one client. Set "
                 "PERMISSIONS_TRUSTED_PROXIES to the proxy's address (e.g. 127.0.0.1 for a local cloudflared) so each client is throttled on its own.")


def admin_key_from_env(env=None) -> str:
    """The grants admin key, or "" when there is none that may be used. ONLY ``PERMISSIONS_ADMIN_KEY`` counts: the fleet-operations key
    (``AGENTIC_OS_API_KEY``) and the control token are separate privileges and administer no grant. A key shorter than
    ``MIN_ADMIN_KEY_LENGTH`` characters is a server misconfiguration, treated exactly like no key (the API answers 503) and logged once,
    without the value (RR-03, RR-04)."""
    global _short_key_logged
    raw = (os.environ if env is None else env).get(ADMIN_KEY_ENV, "")
    if not raw:
        return ""
    if len(raw) < MIN_ADMIN_KEY_LENGTH:
        if not _short_key_logged:
            _short_key_logged = True
            _log.error("%s is set but shorter than %d characters: the permissions admin API stays DISABLED (503) until a longer key is "
                       "configured (the value is not logged)", ADMIN_KEY_ENV, MIN_ADMIN_KEY_LENGTH)
        return ""
    return raw


def log_disabled(what: str, fix: str) -> None:
    """Log once, server-side, why an API answers 503 and how to enable it. The response body names no variable (PP-10)."""
    global _disabled_logged
    if not _disabled_logged:
        _disabled_logged = True
        _log.error("%s is DISABLED (503): %s", what, fix)


def require_admin_key(request: Request, expected: str) -> None:
    """The admin gate. ``expected`` is the usable admin key ("" = none configured, or too short = the route is DISABLED, 503, never open).
    Wrong credentials are 401 (counted against the client; too many are 429), a missing one is 401, compared in constant time."""
    reject_query_credentials(request)
    if not expected:
        log_disabled("the permissions admin API", f"set {ADMIN_KEY_ENV} to a key of at least {MIN_ADMIN_KEY_LENGTH} characters")
        raise HTTPException(503, "the permissions admin API is disabled on this server")
    global _waiters
    client, trustworthy = client_identity(request)
    if trustworthy:
        wait = _locked_for(client)
        if wait:
            raise HTTPException(429, "too many failed attempts; try again later", headers={"Retry-After": str(wait)})
    else:
        delay = _backoff_delay(_failure_count(client))
        if delay:
            with _failures_lock:
                if _waiters >= MAX_BACKOFF_WAITERS:
                    raise HTTPException(429, "too many failed attempts; try again later", headers={"Retry-After": str(max(1, int(BACKOFF_CAP + 0.999)))})
                _waiters += 1
            try:
                _sleep(delay)                      # the same for a correct, a missing and a wrong key: the slow-down tells nothing
            finally:
                with _failures_lock:
                    _waiters -= 1
    presented = presented_credential(request)
    if presented and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        _clear_failures(client)
        return
    if presented:
        _note_failure(client)
    raise HTTPException(401, "invalid or missing admin credentials (X-API-Key or Authorization: Bearer)",
                        headers={"WWW-Authenticate": "Bearer"})


def require_permissions_admin(request: Request) -> None:
    """The ONE admin dependency of every ``/api/permissions/*`` route, control plane and showcase alike."""
    require_admin_key(request, admin_key_from_env())


# ───────────────────────── security headers for the pages and the admin API (PP-02, PP-05, PP-12) ─────────────────────────
BASE_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}
API_CSP = "default-src 'none'; frame-ancestors 'none'"


def page_csp(nonce: str) -> str:
    return (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def secure_page(builder, *args, **kwargs) -> HTMLResponse:
    """An HTML page built with a fresh per-response nonce, carrying the strict CSP and the other security headers. The builder takes ``nonce=``
    and puts it on its inline ``<script>`` and ``<style>``; there is no inline handler and no style attribute, so no 'unsafe-inline'."""
    nonce = secrets.token_urlsafe(18)
    resp = HTMLResponse(builder(*args, nonce=nonce, **kwargs))
    resp.headers.update({**BASE_HEADERS, "Content-Security-Policy": page_csp(nonce), "Cache-Control": "no-store"})
    return resp


class SecurityHeaders:
    """Pure ASGI middleware (no body buffering). Inside ``scope_prefixes`` (None = every path) it adds the base security headers, a
    restrictive CSP to any response that has none, and ``Cache-Control: no-store`` under ``no_store_prefixes``; on EVERY path it rewrites an
    absolute trailing-slash redirect to a relative one, so a Location never echoes the Host header or an ``http`` scheme (PP-05)."""

    def __init__(self, app, *, scope_prefixes=None, no_store_prefixes=()):
        self.app, self.scope_prefixes, self.no_store_prefixes = app, tuple(scope_prefixes) if scope_prefixes is not None else None, tuple(no_store_prefixes)

    @staticmethod
    def _under(path: str, prefixes) -> bool:
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        in_scope = self.scope_prefixes is None or self._under(path, self.scope_prefixes)
        no_store = self._under(path, self.no_store_prefixes)

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                loc = headers.get("location")
                if loc and message["status"] in (301, 302, 303, 307, 308):
                    parts = urlsplit(loc)
                    if parts.path.rstrip("/") == path.rstrip("/") and (parts.scheme or parts.netloc):
                        headers["location"] = parts.path + (("?" + parts.query) if parts.query else "")
                if in_scope:
                    for k, v in BASE_HEADERS.items():
                        headers[k] = v
                    if "content-security-policy" not in headers:
                        headers["Content-Security-Policy"] = API_CSP
                    if no_store:
                        headers["Cache-Control"] = "no-store"
            await send(message)
        await self.app(scope, receive, wrapped)
