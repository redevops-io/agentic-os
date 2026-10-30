"""Authentication, replay protection, rate limiting and the callback-target (SSRF) policy of the RAP bridge.

RAP leaves the runtime-to-tool authentication scheme open (spec: basic/transport, "Authentication"). This package uses one scheme in both
directions, ``RAP-HMAC-SHA256-V1``: an HMAC over a canonical string that binds the method, path, audience, key id, timestamp, nonce and
body hash (and, for a callback, the tenant, principal, original operation, thread, tool call, event id and expiry).

* inbound (runtime -> bridge): the key id selects a :class:`Peer`, which fixes the tenant and the principal. A body can never choose them.
  Every request is checked for signature, clock skew, a never-seen nonce, the audience of this bridge and the scope the path needs.
* outbound (bridge -> runtime): each result carries the same kind of signature, plus an ``event-id`` that is stable across delivery
  retries, so a receiver can drop a duplicate. :class:`CallbackVerifier` is the reference verifier a receiver can use.
* the callback URL a runtime supplies is untrusted input: it must have the exact origin of an entry in the peer's allowlist, carry no
  credentials, use HTTPS (plain HTTP only to a loopback address literal an entry allows), and resolve to addresses the entry permits;
  the connection is pinned to the checked address and redirects are never followed.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from .contracts import MAX_URL_LEN, sha256_hex

SCHEME = "RAP-HMAC-SHA256-V1"
H_KEY, H_TS, H_NONCE, H_SIG, H_AUD = "X-Rap-Key-Id", "X-Rap-Timestamp", "X-Rap-Nonce", "X-Rap-Signature", "X-Rap-Audience"
H_EVENT, H_TENANT, H_PRINCIPAL, H_OP, H_EXP = "X-Rap-Event-Id", "X-Rap-Tenant", "X-Rap-Principal", "X-Rap-Operation", "X-Rap-Expires"
H_GROUP, H_CALL = "X-Rap-Group-Id", "X-Rap-Tool-Call-Id"
SCOPE_INVOKE, SCOPE_CANCEL, SCOPE_CLOSE, SCOPE_DISCOVER = "invoke", "cancel", "close", "discover"
ALL_SCOPES = (SCOPE_INVOKE, SCOPE_CANCEL, SCOPE_CLOSE, SCOPE_DISCOVER)
PATH_SCOPE = {"/invoke": SCOPE_INVOKE, "/cancel_tool_call": SCOPE_CANCEL, "/close_thread": SCOPE_CLOSE, "/.well-known/rap-toolset": SCOPE_DISCOVER}


class AuthError(Exception):
    """Internal reason for a refusal. The peer only ever learns ``status`` (401 / 403 / 429)."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _derive(secret: bytes, direction: str) -> bytes:
    return hmac.new(secret, b"redevops-rap/v1/" + direction.encode(), hashlib.sha256).digest()


def canonical_string(method: str, path: str, audience: str, key_id: str, ts: int, nonce: str, body_sha256: str,
                     extra: Optional[Mapping[str, str]] = None) -> bytes:
    lines = [SCHEME, method.upper(), path, audience, key_id, str(int(ts)), nonce, body_sha256]
    lines += [f"{k}:{v}" for k, v in sorted((extra or {}).items())]
    return "\n".join(lines).encode()


def sign(secret: bytes, direction: str, method: str, path: str, audience: str, key_id: str, ts: int, nonce: str, body: bytes,
         extra: Optional[Mapping[str, str]] = None) -> str:
    return hmac.new(_derive(secret, direction), canonical_string(method, path, audience, key_id, ts, nonce, sha256_hex(body), extra),
                    hashlib.sha256).hexdigest()


# ── peers ────────────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CallbackOrigin:
    """One callback origin a peer may be called back on. Deny by default: no entry, no callback."""
    origin: str                              # scheme://host:port, lowercase, no path
    allow_insecure_loopback: bool = False    # plain http, and only to 127.0.0.1 / ::1 given as an address literal
    allow_private: bool = False              # the host may resolve to a non-public address (an internal runtime)

    def __post_init__(self) -> None:
        u = urllib.parse.urlsplit(self.origin)
        if u.scheme not in ("http", "https") or not u.hostname or u.path not in ("", "/") or u.query or u.fragment or u.username:
            raise ValueError("a callback origin is scheme://host[:port] and nothing else")
        if u.scheme == "http" and not (self.allow_insecure_loopback and _is_loopback_literal(u.hostname)):
            raise ValueError("plain http is only allowed to a loopback address literal with allow_insecure_loopback")

    @property
    def normalized(self) -> str:
        u = urllib.parse.urlsplit(self.origin)
        return f"{u.scheme}://{u.hostname.lower()}:{u.port or (443 if u.scheme == 'https' else 80)}"


@dataclass
class Peer:
    """An authenticated runtime. ``tenant`` and ``principal`` are fixed here and are the only identity the bridge acts under."""
    key_id: str
    secret: bytes = field(repr=False)
    tenant: str
    principal: str
    audience: str
    scopes: FrozenSet[str] = frozenset({SCOPE_INVOKE, SCOPE_CANCEL, SCOPE_CLOSE, SCOPE_DISCOVER})
    operations: Optional[FrozenSet[str]] = None          # None = every registered operation; otherwise an allowlist
    callback_origins: Tuple[CallbackOrigin, ...] = ()
    expires_at: float = 0.0                              # 0 = does not expire
    revoked: bool = False
    gateway_scopes: Tuple[str, ...] = ()                 # OAuth-style scopes handed to the Agent Gateway for this principal
    max_inflight: int = 256
    rate_per_s: float = 50.0
    burst: int = 100

    def __post_init__(self) -> None:
        if len(self.secret) < 16:
            raise ValueError("a peer secret must be at least 16 bytes")
        if not self.tenant or not self.principal or not self.audience:
            raise ValueError("tenant, principal and audience are required")


class NonceCache:
    """Remembers (key id, nonce) for ``ttl_s``. Bounded: when full of live entries a new nonce is REFUSED, never accepted unchecked."""

    def __init__(self, ttl_s: float = 300.0, max_entries: int = 100_000, clock: Callable[[], float] = time.time):
        self.ttl_s, self.max_entries, self.clock = ttl_s, max_entries, clock
        self._seen: Dict[Tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def check_and_add(self, key_id: str, nonce: str) -> bool:
        now = self.clock()
        with self._lock:
            if len(self._seen) >= self.max_entries:
                for k in [k for k, exp in self._seen.items() if exp <= now]:
                    del self._seen[k]
                if len(self._seen) >= self.max_entries:
                    return False
            k = (key_id, nonce)
            exp = self._seen.get(k)
            if exp is not None and exp > now:
                return False
            self._seen[k] = now + self.ttl_s
            return True


class RateLimiter:
    """Token bucket per key."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000):
        self.clock, self.max_keys = clock, max_keys
        self._b: Dict[str, Tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, rate_per_s: float, burst: int) -> bool:
        now = self.clock()
        with self._lock:
            if key not in self._b and len(self._b) >= self.max_keys:
                self._b.clear()                       # bounded memory: a flood of distinct keys resets the table, it never grows it
            tokens, last = self._b.get(key, (float(burst), now))
            tokens = min(float(burst), tokens + (now - last) * rate_per_s)
            if tokens < 1.0:
                self._b[key] = (tokens, now)
                return False
            self._b[key] = (tokens - 1.0, now)
            return True


class PeerRegistry:
    """Peers by key id, plus the inbound authenticator."""

    def __init__(self, audience: str, *, clock: Callable[[], float] = time.time, skew_s: float = 60.0,
                 nonces: Optional[NonceCache] = None):
        self.audience, self.clock, self.skew_s = audience, clock, skew_s
        self.nonces = nonces or NonceCache(ttl_s=skew_s * 2 + 5, clock=clock)
        self._peers: Dict[str, Peer] = {}

    def add(self, peer: Peer) -> Peer:
        if peer.audience != self.audience:
            raise ValueError("a peer's audience must be this bridge's audience")
        self._peers[peer.key_id] = peer
        return peer

    def get(self, key_id: str) -> Optional[Peer]:
        return self._peers.get(key_id)

    def revoke(self, key_id: str) -> None:
        p = self._peers.get(key_id)
        if p is not None:
            p.revoked = True

    def authenticate(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> Peer:
        """Raises :class:`AuthError` (401 for anything about identity, 403 for a missing scope). Never reveals which check failed."""
        h = {k.lower(): v for k, v in headers.items()}
        key_id, ts_s, nonce, sig, aud = (h.get(x.lower(), "") for x in (H_KEY, H_TS, H_NONCE, H_SIG, H_AUD))
        if not (key_id and ts_s and nonce and sig) or len(nonce) > 128 or len(key_id) > 128 or len(sig) != 64:
            raise AuthError(401, "missing_or_malformed_headers")
        peer = self._peers.get(key_id)
        # compute the signature even for an unknown key so the response time does not separate "unknown key" from "bad signature"
        secret = peer.secret if peer is not None else b"\x00" * 32
        try:
            ts = int(ts_s)
        except ValueError:
            raise AuthError(401, "bad_timestamp") from None
        expected = sign(secret, "inbound", method, path, self.audience, key_id, ts, nonce, body)
        if not hmac.compare_digest(expected, sig.lower()):
            raise AuthError(401, "bad_signature")
        if peer is None or peer.revoked:
            raise AuthError(401, "unknown_or_revoked_key")
        now = self.clock()
        if peer.expires_at and now >= peer.expires_at:
            raise AuthError(401, "key_expired")
        if abs(now - ts) > self.skew_s:
            raise AuthError(401, "clock_skew")
        if aud and aud != self.audience:
            raise AuthError(401, "wrong_audience")
        if not self.nonces.check_and_add(key_id, nonce):
            raise AuthError(401, "replayed_or_unrecordable_nonce")
        scope = PATH_SCOPE.get(path)
        if scope is None or scope not in peer.scopes:
            raise AuthError(403, "scope")
        return peer


def sign_request(peer_secret: bytes, key_id: str, audience: str, method: str, path: str, body: bytes, *, ts: Optional[int] = None,
                 nonce: Optional[str] = None) -> Dict[str, str]:
    """Client side of the inbound scheme (used by tests, the emulated runtime and as the specification for a runtime's HttpClient)."""
    import secrets as _s
    ts = int(time.time()) if ts is None else ts
    nonce = nonce or _s.token_hex(16)
    return {H_KEY: key_id, H_TS: str(ts), H_NONCE: nonce, H_AUD: audience,
            H_SIG: sign(peer_secret, "inbound", method, path, audience, key_id, ts, nonce, body)}


# ── outbound callbacks ────────────────────────────────────────────────────────────────────────────────────────────
def callback_headers(peer: Peer, *, url: str, body: bytes, event_id: str, operation: str, group_id: str, tool_call_id: str, expires_at: float,
                     now: Optional[float] = None, nonce: Optional[str] = None) -> Dict[str, str]:
    """Headers for one delivery attempt. ``event_id`` is the same on every retry of one message; timestamp and nonce are fresh."""
    import secrets as _s
    ts = int(time.time() if now is None else now)
    nonce = nonce or _s.token_hex(16)
    u = urllib.parse.urlsplit(url)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    audience = _origin_of(url)
    extra = {"event": event_id, "tenant": peer.tenant, "principal": peer.principal, "operation": operation, "group": group_id,
             "call": tool_call_id, "expires": str(int(expires_at))}
    sig = sign(peer.secret, "callback", "POST", path, audience, peer.key_id, ts, nonce, body, extra)
    return {H_KEY: peer.key_id, H_TS: str(ts), H_NONCE: nonce, H_AUD: audience, H_EVENT: event_id, H_TENANT: peer.tenant,
            H_PRINCIPAL: peer.principal, H_OP: operation, H_GROUP: group_id, H_CALL: tool_call_id, H_EXP: str(int(expires_at)), H_SIG: sig}


class CallbackVerifier:
    """Reference receiver-side check of a callback, for a runtime or an emulated peer.

    ``verify`` returns ``("ok"|"duplicate", headers-derived claims)`` or raises :class:`AuthError`. A duplicate (same event id, valid
    signature) is acknowledged by the receiver but must not be applied twice. Replays of the same nonce, an expired message, another audience,
    another tenant or a body that does not match the signature are refused."""

    def __init__(self, secret: bytes, key_id: str, audience: str, *, expect_tenant: str, clock: Callable[[], float] = time.time,
                 skew_s: float = 60.0, max_events: int = 100_000):
        self.secret, self.key_id, self.audience, self.expect_tenant = secret, key_id, audience, expect_tenant
        self.clock, self.skew_s, self.max_events = clock, skew_s, max_events
        self.nonces = NonceCache(ttl_s=skew_s * 2 + 5, clock=clock)
        self._events: Dict[str, str] = {}
        self._lock = threading.Lock()

    def verify(self, path_and_query: str, headers: Mapping[str, str], body: bytes) -> Tuple[str, Dict[str, str]]:
        h = {k.lower(): v for k, v in headers.items()}
        g = lambda name: h.get(name.lower(), "")  # noqa: E731
        sig = g(H_SIG)
        if g(H_KEY) != self.key_id or len(sig) != 64 or not g(H_NONCE) or not g(H_EVENT):
            raise AuthError(401, "malformed")
        try:
            ts, exp = int(g(H_TS)), int(g(H_EXP))
        except ValueError:
            raise AuthError(401, "malformed") from None
        extra = {"event": g(H_EVENT), "tenant": g(H_TENANT), "principal": g(H_PRINCIPAL), "operation": g(H_OP), "group": g(H_GROUP),
                 "call": g(H_CALL), "expires": str(exp)}
        expected = sign(self.secret, "callback", "POST", path_and_query, self.audience, self.key_id, ts, g(H_NONCE), body, extra)
        if not hmac.compare_digest(expected, sig.lower()):
            raise AuthError(401, "bad_signature")
        if g(H_AUD) != self.audience:
            raise AuthError(401, "wrong_audience")
        if g(H_TENANT) != self.expect_tenant:
            raise AuthError(403, "tenant")
        now = self.clock()
        if abs(now - ts) > self.skew_s:
            raise AuthError(401, "clock_skew")
        if now >= exp:
            raise AuthError(401, "expired")
        if not self.nonces.check_and_add(self.key_id, g(H_NONCE)):
            raise AuthError(401, "replayed_nonce")
        digest = sha256_hex(body)
        with self._lock:
            prev = self._events.get(g(H_EVENT))
            if prev is not None:
                if prev != digest:
                    raise AuthError(409, "event_id_reused_with_different_body")
                return "duplicate", extra
            if len(self._events) >= self.max_events:
                raise AuthError(503, "event_table_full")
            self._events[g(H_EVENT)] = digest
        return "ok", extra


# ── callback target policy (SSRF) ─────────────────────────────────────────────────────────────────────────────────
def _is_loopback_literal(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _origin_of(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{(u.hostname or '').lower()}:{u.port or (443 if u.scheme == 'https' else 80)}"


def _public(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global and not ip.is_multicast)


Resolver = Callable[[str], Iterable[str]]


def _system_resolver(host: str) -> List[str]:
    return [ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


class CallbackRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def check_callback_url(url: Any, origins: Tuple[CallbackOrigin, ...], resolver: Optional[Resolver] = None) -> Tuple[str, List[str]]:
    """Returns (normalised origin, addresses the connection may use) or raises :class:`CallbackRefused`.

    Deny by default: the origin must equal an allowlist entry exactly. Credentials, fragments, control characters, non-ASCII hosts, hosts
    written as numbers in a non-canonical form and over-long URLs are refused before any lookup."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LEN or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise CallbackRefused("invalid_url")
    try:
        u = urllib.parse.urlsplit(url)
        host, port = (u.hostname or ""), u.port
    except ValueError:
        raise CallbackRefused("invalid_url") from None
    if u.scheme not in ("http", "https") or not host or u.username is not None or u.password is not None or u.fragment:
        raise CallbackRefused("invalid_url")
    if not host.isascii() or host.endswith("."):
        raise CallbackRefused("invalid_host")
    try:
        lit = ipaddress.ip_address(host)
        canonical = str(lit) == host.lower() or (isinstance(lit, ipaddress.IPv6Address))
        if not canonical:
            raise CallbackRefused("non_canonical_address")
    except ValueError:
        lit = None
        if all(p.isdigit() or p.lower().startswith("0x") for p in host.split(".")):
            raise CallbackRefused("non_canonical_address")
    origin = _origin_of(url)
    entry = next((o for o in origins if o.normalized == origin), None)
    if entry is None:
        raise CallbackRefused("origin_not_allowlisted")
    if u.scheme == "http" and not (entry.allow_insecure_loopback and lit is not None and lit.is_loopback):
        raise CallbackRefused("insecure_scheme")
    addrs = [str(lit)] if lit is not None else None
    if addrs is None:
        try:
            addrs = [a.split("%")[0] for a in (resolver or _system_resolver)(host)]
        except Exception:  # noqa: BLE001 - unresolvable: fail closed
            raise CallbackRefused("unresolvable") from None
    if not addrs:
        raise CallbackRefused("unresolvable")
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            raise CallbackRefused("bad_address") from None
        ok = _public(ip) or entry.allow_private or (entry.allow_insecure_loopback and ip.is_loopback and lit is not None)
        if not ok:
            raise CallbackRefused("non_public_address")
    return origin, addrs


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


@dataclass(frozen=True)
class PostOutcome:
    kind: str        # delivered | retry | permanent | refused
    status: int = 0
    detail: str = ""


class CallbackClient:
    """POSTs one callback. Connects only to an address :func:`check_callback_url` approved, never follows a redirect, reads at most 1 KiB of reply."""

    def __init__(self, resolver: Optional[Resolver] = None, timeout_s: float = 10.0, tls_context: Optional[ssl.SSLContext] = None):
        self.resolver, self.timeout_s = resolver, timeout_s
        self.tls = tls_context or ssl.create_default_context()

    def post(self, url: str, body: bytes, headers: Mapping[str, str], origins: Tuple[CallbackOrigin, ...]) -> PostOutcome:
        try:
            origin, addrs = check_callback_url(url, origins, self.resolver)
        except CallbackRefused as e:
            return PostOutcome("refused", 0, e.reason)
        u = urllib.parse.urlsplit(url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
        target = (u.path or "/") + (("?" + u.query) if u.query else "")
        hdrs = {"Content-Type": "application/json", "Content-Length": str(len(body)), **dict(headers)}
        last = "connect_failed"
        for ip in addrs:
            conn = _PinnedHTTPS(host, port, ip, self.timeout_s, self.tls) if u.scheme == "https" else _PinnedHTTP(host, port, ip, self.timeout_s)
            try:
                conn.request("POST", target, body=body, headers=hdrs)
                resp = conn.getresponse()
                resp.read(1024)
                st = resp.status
            except (OSError, http.client.HTTPException, ssl.SSLError) as e:
                last = type(e).__name__
                continue
            finally:
                conn.close()
            if 200 <= st < 300:
                return PostOutcome("delivered", st)
            if 300 <= st < 400:
                return PostOutcome("refused", st, "redirect_not_followed")
            if st in (408, 425, 429) or st >= 500:
                return PostOutcome("retry", st)
            return PostOutcome("permanent", st)
        return PostOutcome("retry", 0, last)
