"""Local capability gate for the desktop sidecar's loopback HTTP API.

Why this exists: the sidecar listens on 127.0.0.1 and used to answer ANY caller, with
``Access-Control-Allow-Origin: *``. Any web page open in the user's browser could therefore make a
cross-origin anonymous POST to ``/api/sources/confirm`` and have the sidecar walk a local folder.
Loopback is not an access control: every local process and every web page in the user's browser can
reach it. This module makes the API deny-by-default and reachable only with a *capability* that the
launcher (or, standalone, the person who started ``serve``) hands to the browser out-of-band.

Model (full spec: ``docs/LOCAL_API_SECURITY.md``):

* A per-launch **secret** (256 bits, ``secrets``) is generated in memory by the sidecar. It is given to
  the launcher once, over an out-of-band channel, and never travels over HTTP: the launcher proves it
  holds it (and checks the server holds it) with an HMAC challenge, so a stale or foreign process on
  the port cannot be adopted.
* A short-lived, single-use **launch code** is placed by the launcher in the URL *fragment*. The UI
  script exchanges it (POST, JSON) for a **session**: an HttpOnly + SameSite=Strict host-only cookie
  plus a **CSRF token** that must be echoed in a custom header on every state-changing request.
* Defence in depth on every request: strict ``Host`` allowlist (DNS rebinding), ``Origin`` must be
  absent or same-origin, ``Sec-Fetch-Site`` honoured, preflights refused, **no** CORS headers ever,
  security headers and ``Cache-Control: no-store`` on API responses.

Everything is a pure ASGI middleware so it covers every route (including ``/docs``, ``/redoc``,
``/openapi.json``, conditionally mounted routers, and 404s) and is wrapped around the OUTERMOST app.

What this does NOT do: protect against malware running as the same OS user (it can read the
launcher's memory or the browser's cookie jar). See the doc.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

PROTO = "agentic-os-local-capability/1"
COOKIE_NAME = "agentic_os_session"
CSRF_HEADER = "x-agentic-csrf"          # lower-case: ASGI header names are lower-case
SESSION_PATH = "/api/local/session"
HANDSHAKE_PATH = "/api/local/handshake"
LAUNCH_CODE_PATH = "/api/local/launch-code"
LOCAL_PATHS = frozenset({SESSION_PATH, HANDSHAKE_PATH, LAUNCH_CODE_PATH})
# Paths FastAPI registers itself; never allowed to be public even if a file of that name is vendored.
_RESERVED_PUBLIC = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})
_SAFE_METHODS = frozenset({"GET", "HEAD"})
_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")
_HOST_ANY_PORT = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_BODY = 4096


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def handshake_proof(secret: bytes, port: int, client_nonce: str) -> str:
    """HMAC the server returns to prove it holds the launch secret (domain-separated)."""
    msg = f"agentic-os-handshake-v1\n{int(port)}\n{client_nonce}".encode("ascii")
    return b64url(hmac.new(secret, msg, hashlib.sha256).digest())


def mint_proof(secret: bytes, port: int, server_nonce: str) -> str:
    """HMAC the launcher sends to prove it holds the launch secret when asking for a new launch code."""
    msg = f"agentic-os-mint-v1\n{int(port)}\n{server_nonce}".encode("ascii")
    return b64url(hmac.new(secret, msg, hashlib.sha256).digest())


def _ct_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


@dataclass
class Session:
    csrf: str
    expires: float


class _FailureLimiter:
    """Sliding-window limit on FAILED credential attempts (launch-code exchange, launch-code mint).
    Loopback has a single client address, so this is global, not per-peer: with 256-bit codes it is
    noise-suppression and a guess cap, not the control that makes guessing infeasible."""

    def __init__(self, clock: Callable[[], float], max_failures: int, window: float):
        self._clock, self._max, self._window = clock, max_failures, window
        self._failures: Deque[float] = deque()

    def retry_after(self) -> int:
        now = self._clock()
        while self._failures and now - self._failures[0] >= self._window:
            self._failures.popleft()
        if len(self._failures) >= self._max:
            return max(1, int(self._failures[0] + self._window - now) + 1)
        return 0

    def record_failure(self) -> None:
        self._failures.append(self._clock())


class LocalCapability:
    """In-memory state: the launch secret, outstanding launch codes, handshake challenges, sessions.

    Nothing here is ever persisted. The secret cannot be supplied by the caller: it is generated in
    ``__init__`` and only leaves through :meth:`launcher_handoff` (the out-of-band record)."""

    MAX_CODES = 8
    MAX_SESSIONS = 16
    MAX_CHALLENGES = 16

    def __init__(self, *, port: Optional[int] = None, extra_hosts: Iterable[str] = (),
                 launch_code_ttl: float = 60.0, session_ttl: float = 86400.0,
                 challenge_ttl: float = 30.0, max_failures: int = 10, failure_window: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self._secret = secrets.token_bytes(32)           # 256 bits, memory only
        self.port = port
        self.extra_hosts = frozenset(h.strip().lower() for h in extra_hosts if h and h.strip())
        self.launch_code_ttl = float(launch_code_ttl)
        self.session_ttl = float(session_ttl)
        self.challenge_ttl = float(challenge_ttl)
        self._clock = clock
        self._lock = threading.Lock()
        self._codes: List[Tuple[bytes, float]] = []
        self._challenges: List[Tuple[bytes, float]] = []
        self._sessions: List[Tuple[bytes, Session]] = []
        # Two independent budgets: noise on the launch-code exchange (any local process can send
        # it) must never lock the launcher out of minting codes.
        self.limiter = _FailureLimiter(clock, max_failures, failure_window)        # POST /api/local/session
        self.mint_limiter = _FailureLimiter(clock, max_failures, failure_window)   # POST /api/local/launch-code

    # ------------------------------------------------------------------ hand-off to the launcher
    def bind_port(self, port: int) -> None:
        self.port = int(port)

    def launcher_handoff(self, host: str, port: int, base_path: str = "",
                         launch_code_ttl: Optional[float] = None,
                         include_launch_code: bool = True) -> Dict[str, Any]:
        """The one-time out-of-band record for the launcher. Contains the secret: never log it.
        ``launch_code`` and ``launch_code_ttl`` are optional on the wire: a launcher that mints its own
        codes (4.4) does not need the first one, and an unused code holds one of the 8 slots for its TTL."""
        import os
        self.bind_port(port)
        rec: Dict[str, Any] = {"v": 1, "proto": PROTO, "event": "ready", "host": host, "port": int(port),
                               "pid": os.getpid(), "base_path": base_path, "secret": b64url(self._secret)}
        if include_launch_code:
            ttl = self.launch_code_ttl if launch_code_ttl is None else float(launch_code_ttl)
            rec["launch_code"] = self.issue_launch_code(ttl)
            rec["launch_code_ttl"] = int(ttl)
        return rec

    # ------------------------------------------------------------------ Host / Origin
    def host_ok(self, host: str) -> bool:
        h = (host or "").strip().lower()
        if not h:
            return False
        if h in self.extra_hosts:
            return True
        if self.port is None:
            return bool(_HOST_ANY_PORT.match(h))
        return any(h == f"{n}:{self.port}" or (self.port == 80 and h == n) for n in _LOOPBACK_NAMES)

    @staticmethod
    def origin_ok(origin: str, host: str) -> bool:
        """Same-origin only: the Origin must be exactly ``http://<the Host header we accepted>``."""
        return origin.strip().lower() == "http://" + host.strip().lower()

    # ------------------------------------------------------------------ launch codes
    def issue_launch_code(self, ttl: Optional[float] = None) -> str:
        code = secrets.token_urlsafe(32)
        expiry = self._clock() + (self.launch_code_ttl if ttl is None else float(ttl))
        with self._lock:
            self._purge(self._codes)
            if len(self._codes) >= self.MAX_CODES:
                self._codes.pop(0)
            self._codes.append((_digest(code), expiry))
        return code

    def consume_launch_code(self, code: str) -> bool:
        """Atomically check and burn a code. Compares against every outstanding digest (no early exit)."""
        if not isinstance(code, str) or not _B64URL.match(code) or len(code) > 128:
            return False
        d = _digest(code)
        with self._lock:
            now = self._clock()
            hit = -1
            for i, (stored, _exp) in enumerate(self._codes):
                if hmac.compare_digest(stored, d):
                    hit = i
            if hit < 0:
                return False
            _stored, expiry = self._codes.pop(hit)          # burned whether or not it is still valid
            return now < expiry

    # ------------------------------------------------------------------ sessions
    def new_session(self) -> Tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        sess = Session(csrf=secrets.token_urlsafe(32), expires=self._clock() + self.session_ttl)
        with self._lock:
            self._purge_sessions()
            if len(self._sessions) >= self.MAX_SESSIONS:
                self._sessions.pop(0)
            self._sessions.append((_digest(token), sess))
        return token, sess

    def find_session(self, tokens: Iterable[str]) -> Optional[Session]:
        with self._lock:
            self._purge_sessions()
            found: Optional[Session] = None
            for tok in tokens:
                if not isinstance(tok, str) or len(tok) > 128:
                    continue
                d = _digest(tok)
                for stored, sess in self._sessions:
                    if hmac.compare_digest(stored, d):
                        found = sess
            return found

    def drop_session(self, sess: Session) -> None:
        with self._lock:
            self._sessions = [(d, s) for d, s in self._sessions if s is not sess]

    def _purge_sessions(self) -> None:
        now = self._clock()
        self._sessions = [(d, s) for d, s in self._sessions if now < s.expires]

    def _purge(self, store: List[Tuple[bytes, float]]) -> None:
        now = self._clock()
        store[:] = [(d, e) for d, e in store if now < e]

    # ------------------------------------------------------------------ handshake + mint
    def handshake(self, client_nonce: str) -> Optional[Dict[str, Any]]:
        if not (isinstance(client_nonce, str) and _B64URL.match(client_nonce) and 22 <= len(client_nonce) <= 86):
            return None
        server_nonce = b64url(secrets.token_bytes(16))
        with self._lock:
            self._purge(self._challenges)
            if len(self._challenges) >= self.MAX_CHALLENGES:
                self._challenges.pop(0)
            self._challenges.append((_digest(server_nonce), self._clock() + self.challenge_ttl))
        return {"v": 1, "proto": PROTO, "proof": handshake_proof(self._secret, self.port or 0, client_nonce),
                "server_nonce": server_nonce, "expires_in": int(self.challenge_ttl)}

    def mint_launch_code(self, server_nonce: str, proof: str) -> Optional[str]:
        """Issue a fresh launch code to a caller that proves possession of the secret. The challenge
        is burned on every attempt, right or wrong, so a proof cannot be replayed or ground online."""
        if not (isinstance(server_nonce, str) and _B64URL.match(server_nonce) and len(server_nonce) <= 64):
            return None
        d = _digest(server_nonce)
        with self._lock:
            now = self._clock()
            hit = -1
            for i, (stored, _e) in enumerate(self._challenges):
                if hmac.compare_digest(stored, d):
                    hit = i
            if hit < 0:
                return None
            _stored, expiry = self._challenges.pop(hit)
            if now >= expiry:
                return None
        expected = mint_proof(self._secret, self.port or 0, server_nonce)
        if not isinstance(proof, str) or not _ct_equal(proof, expected):
            return None
        return self.issue_launch_code()


# ====================================================================== ASGI middleware
_Headers = List[Tuple[bytes, bytes]]


def _security_headers(no_store: bool) -> _Headers:
    h: _Headers = [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-frame-options", b"DENY"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
        (b"content-security-policy",
         b"default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
         b"font-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
         b"frame-ancestors 'none'; object-src 'none'"),
    ]
    if no_store:
        h += [(b"cache-control", b"no-store"), (b"pragma", b"no-cache")]
    return h


def _finish_headers(existing: _Headers, no_store: bool) -> _Headers:
    """Drop every CORS header the app may have set; add ours (ours win for cache-control)."""
    ours = _security_headers(no_store)
    override = {n for n, _ in ours} if no_store else {n for n, _ in ours if n != b"cache-control"}
    kept = [(k, v) for k, v in existing
            if not k.lower().startswith(b"access-control-") and k.lower() not in override]
    return kept + ours


def public_ui_paths(ui_dir: Any, extra: Iterable[str] = ()) -> frozenset:
    """Exact URL paths of the static UI files (plus ``extra``): the only unauthenticated GET surface.
    Dot-files, ``/api/...`` and FastAPI's reserved paths are never public."""
    from pathlib import Path
    paths = set(extra)
    root = Path(ui_dir)
    for f in root.rglob("*"):
        try:
            if not f.is_file():
                continue
            rel = f.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        if any(part.startswith(".") for part in rel.split("/")):
            continue
        p = "/" + rel
        if p.startswith("/api/") or p in _RESERVED_PUBLIC:
            continue
        paths.add(p)
    return frozenset(paths)


class LocalCapabilityMiddleware:
    """Wrap the OUTERMOST ASGI app. ``base_path`` is the URL prefix the app is mounted under (or "")."""

    def __init__(self, app: Any, capability: LocalCapability, *, base_path: str = "",
                 public_paths: Iterable[str] = ()):
        self.app = app
        self.cap = capability
        self.base = (base_path or "").rstrip("/")
        self.public = frozenset(p for p in public_paths if p not in _RESERVED_PUBLIC)
        self.cookie_path = self.base or "/"

    # -------------------------------------------------------------- entry
    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await self.app(scope, receive, send)
            return
        if kind == "websocket":                      # no websocket route is part of the product
            await send({"type": "websocket.close", "code": 1008})
            return
        if kind != "http":
            return
        await self._http(scope, receive, send)

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _headers(scope: Dict[str, Any]) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for k, v in scope.get("headers", []):
            out.setdefault(k.decode("latin-1").lower(), []).append(v.decode("latin-1"))
        return out

    async def _reply(self, send: Callable, status: int, code: str, message: str,
                     extra: Optional[_Headers] = None, body: Optional[Dict[str, Any]] = None) -> None:
        payload = json.dumps(body if body is not None else {"error": {"code": code, "message": message}},
                             separators=(",", ":")).encode("utf-8")
        headers: _Headers = [(b"content-type", b"application/json"),
                             (b"content-length", str(len(payload)).encode("ascii"))]
        headers += extra or []
        await send({"type": "http.response.start", "status": status,
                    "headers": _finish_headers(headers, no_store=True)})
        await send({"type": "http.response.body", "body": payload})

    async def _read_json(self, receive: Callable) -> Optional[Dict[str, Any]]:
        chunks, total = [], 0
        while True:
            msg = await receive()
            if msg["type"] != "http.request":
                return None
            part = msg.get("body", b"")
            total += len(part)
            if total > _MAX_BODY:
                return None
            chunks.append(part)
            if not msg.get("more_body"):
                break
        try:
            data = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _rel(self, path: str) -> Optional[str]:
        if not self.base:
            return path
        if path == self.base:
            return ""
        if path.startswith(self.base + "/"):
            return path[len(self.base):]
        return None

    @staticmethod
    def _cookie_values(hdrs: Dict[str, List[str]]) -> List[str]:
        vals: List[str] = []
        for line in hdrs.get("cookie", []):
            for part in line.split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE_NAME and value:
                    vals.append(value)
        return vals

    # -------------------------------------------------------------- main flow
    async def _http(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        hdrs = self._headers(scope)
        method = scope["method"].upper()
        rel = self._rel(scope["path"])

        # 1. Host allowlist (DNS rebinding): exactly one Host header, and it must be on the list.
        hosts = hdrs.get("host", [])
        if len(hosts) != 1 or not self.cap.host_ok(hosts[0]):
            await self._reply(send, 400, "invalid_host", "Host header not allowed")
            return
        host = hosts[0].strip()

        # 2. The static UI (and nothing else) is public.
        if method in _SAFE_METHODS and rel is not None and (rel in self.public or rel == ""):
            await self.app(scope, receive, self._wrap_send(send, no_store=False))
            return

        # 3. Everything else is gated. Preflights are refused outright: no CORS is ever offered.
        if method == "OPTIONS" and "access-control-request-method" in hdrs:
            await self._reply(send, 403, "cors_not_supported", "cross-origin requests are not supported")
            return
        origins = hdrs.get("origin", [])
        if origins and (len(origins) != 1 or not self.cap.origin_ok(origins[0], host)):
            await self._reply(send, 403, "origin_not_allowed", "cross-origin requests are not allowed")
            return
        sfs = [v.strip().lower() for v in hdrs.get("sec-fetch-site", [])]
        if sfs and not (len(sfs) == 1 and (sfs[0] == "same-origin" or (sfs[0] == "none" and method in _SAFE_METHODS))):
            await self._reply(send, 403, "fetch_site_not_allowed", "cross-site requests are not allowed")
            return

        if rel in LOCAL_PATHS:
            await self._local(rel, method, hdrs, receive, send)
            return

        # 4. Capability: a valid session, plus the CSRF header on anything that can change state.
        sess = self.cap.find_session(self._cookie_values(hdrs))
        if sess is None:
            await self._reply(send, 401, "auth_required", "a local session is required")
            return
        if method not in _SAFE_METHODS:
            presented = hdrs.get(CSRF_HEADER, [])
            if len(presented) != 1 or not _ct_equal(presented[0], sess.csrf):
                await self._reply(send, 403, "csrf_invalid", "missing or invalid CSRF header")
                return
        await self.app(scope, receive, self._wrap_send(send, no_store=True))

    def _wrap_send(self, send: Callable, *, no_store: bool) -> Callable:
        async def wrapped(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = _finish_headers(list(message.get("headers", [])), no_store)
            await send(message)
        return wrapped

    # -------------------------------------------------------------- /api/local/*
    async def _local(self, rel: str, method: str, hdrs: Dict[str, List[str]], receive: Callable,
                     send: Callable) -> None:
        cap = self.cap
        if rel == SESSION_PATH and method == "GET":
            sess = cap.find_session(self._cookie_values(hdrs))
            if sess is None:
                await self._reply(send, 401, "auth_required", "a local session is required")
                return
            await self._reply(send, 200, "", "", body={"v": 1, "csrf_token": sess.csrf, "expires_in": max(
                0, int(sess.expires - cap._clock()))})
            return
        if rel == SESSION_PATH and method == "DELETE":
            sess = cap.find_session(self._cookie_values(hdrs))
            if sess is None:
                await self._reply(send, 401, "auth_required", "a local session is required")
                return
            presented = hdrs.get(CSRF_HEADER, [])
            if len(presented) != 1 or not _ct_equal(presented[0], sess.csrf):
                await self._reply(send, 403, "csrf_invalid", "missing or invalid CSRF header")
                return
            cap.drop_session(sess)
            clear = f"{COOKIE_NAME}=; Path={self.cookie_path}; Max-Age=0; HttpOnly; SameSite=Strict"
            await self._reply(send, 200, "", "", body={"ok": True},
                              extra=[(b"set-cookie", clear.encode("ascii"))])
            return
        if method != "POST":
            await self._reply(send, 405, "method_not_allowed", "method not allowed",
                              extra=[(b"allow", b"POST" if rel != SESSION_PATH else b"GET, POST, DELETE")])
            return

        # POST endpoints: JSON only (a non-simple content type, so a cross-site form cannot send it).
        ctype = (hdrs.get("content-type", [""])[0] or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            await self._reply(send, 415, "unsupported_media_type", "Content-Type must be application/json")
            return
        if rel == SESSION_PATH:
            retry = cap.limiter.retry_after()
            if retry:
                await self._reply(send, 429, "rate_limited", "too many failed attempts",
                                  extra=[(b"retry-after", str(retry).encode("ascii"))])
                return
        data = await self._read_json(receive)
        if data is None:
            if rel == SESSION_PATH:
                cap.limiter.record_failure()
            elif rel == LAUNCH_CODE_PATH:
                cap.mint_limiter.record_failure()
            await self._reply(send, 400, "bad_request", "malformed JSON body")
            return

        if rel == HANDSHAKE_PATH:
            out = cap.handshake(str(data.get("client_nonce", "")))
            if out is None:
                await self._reply(send, 400, "bad_request", "client_nonce must be 22-86 base64url characters")
                return
            await self._reply(send, 200, "", "", body=out)
            return

        if rel == LAUNCH_CODE_PATH:
            # The proof is checked even while the failure budget is spent: it is an HMAC over a
            # single-use server challenge, so a correct one cannot be guessed and a legitimate launcher
            # is never locked out by someone else's bad attempts. Only a FAILED attempt is throttled.
            proof = hdrs.get("x-launcher-proof", [""])[0]
            retry = cap.mint_limiter.retry_after()
            code = cap.mint_launch_code(str(data.get("server_nonce", "")), proof)
            if code is None:
                if retry:
                    await self._reply(send, 429, "rate_limited", "too many failed attempts",
                                      extra=[(b"retry-after", str(retry).encode("ascii"))])
                    return
                cap.mint_limiter.record_failure()
                await self._reply(send, 401, "invalid_proof", "challenge unknown, expired, or proof wrong")
                return
            await self._reply(send, 200, "", "", body={"v": 1, "launch_code": code,
                                                        "expires_in": int(cap.launch_code_ttl)})
            return

        # SESSION_PATH POST: exchange a launch code for a session.
        if not cap.consume_launch_code(str(data.get("code", ""))):
            cap.limiter.record_failure()
            await self._reply(send, 401, "invalid_launch_code", "launch code invalid, expired, or already used")
            return
        token, sess = cap.new_session()
        cookie = f"{COOKIE_NAME}={token}; Path={self.cookie_path}; HttpOnly; SameSite=Strict"
        await self._reply(send, 200, "", "", extra=[(b"set-cookie", cookie.encode("ascii"))],
                          body={"v": 1, "csrf_token": sess.csrf, "csrf_header": "X-Agentic-CSRF",
                                "expires_in": int(cap.session_ttl)})


def install(app: Any, capability: LocalCapability, *, base_path: str = "",
            public_paths: Iterable[str] = ()) -> None:
    """Wrap ``app`` (a Starlette/FastAPI instance: the OUTERMOST one) with the capability gate."""
    app.add_middleware(LocalCapabilityMiddleware, capability=capability, base_path=base_path,
                       public_paths=tuple(public_paths))
