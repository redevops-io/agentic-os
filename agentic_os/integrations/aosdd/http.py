"""HTTP for the AOS-DD adapters: one transport seam, error classification, and a read-only client.

* ``Transport.send`` is the ONLY place a socket is opened. Tests inject a fake; reconstruction runs inside
  ``network_disabled()`` so that any connector call raises ``NetworkTouched`` (proof that rebuilding a finished mission from
  its ledger makes no connector/tool call).
* ``AppError`` carries a classification the runtime understands: ``not_applied`` is True only for answers that prove the
  request did not take effect (credential / permission / validation / duplicate / conflict). A timeout, a transport failure
  or a 5xx is ``unavailable``/``server``: the effect may exist, so the runtime parks the step as UNKNOWN instead of failing it.
* ``AppHttp`` builds the auth header at send time from a ``Secret``; ``read_only=True`` refuses any method but GET, which is
  how the verifier is kept on a code path that cannot write.
"""
from __future__ import annotations

import contextlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .config import Secret
from .redact import NULL, Redactor

DEFINITE = ("credential", "permission", "rejected", "duplicate", "conflict", "not_found")
AMBIGUOUS = ("unavailable", "server")


class NetworkTouched(RuntimeError):
    """A connector/network call was attempted while the network layer was disabled (reconstruction must not make any)."""


class ReadOnlyViolation(RuntimeError):
    pass


class AppError(RuntimeError):
    def __init__(self, app: str, kind: str, status: int, message: str, *, op: str = ""):
        super().__init__(f"{app}: {kind} (HTTP {status}) {message}".strip())
        self.app, self.kind, self.status, self.op = app, kind, status, op
        self.message = message
        #: the runtime reads this attribute (``MissionRuntime._fail_node``): True releases the reservation as "did not execute"
        self.not_applied = kind in DEFINITE


@dataclass
class Resp:
    status: int
    body: Any
    raw: str = ""
    elapsed: float = 0.0
    headers: dict = field(default_factory=dict)


@dataclass
class Request:
    method: str
    url: str
    headers: dict
    body: "bytes | None"
    timeout: float


class Transport(Protocol):
    def send(self, req: Request) -> Resp: ...


_NET = threading.local()
_NET_DISABLED = 0
_NET_LOCK = threading.Lock()


@contextlib.contextmanager
def network_disabled():
    """Within this block every transport raises ``NetworkTouched`` (process-wide, so worker threads are covered)."""
    global _NET_DISABLED
    with _NET_LOCK:
        _NET_DISABLED += 1
    try:
        yield
    finally:
        with _NET_LOCK:
            _NET_DISABLED -= 1


def _guard() -> None:
    if _NET_DISABLED:
        raise NetworkTouched("a connector call was attempted while the network layer is disabled")


class UrllibTransport:
    def send(self, req: Request) -> Resp:
        _guard()
        r = urllib.request.Request(req.url, req.body, req.headers, method=req.method)
        t = time.time()
        try:
            x = urllib.request.build_opener().open(r, timeout=req.timeout)
            raw, st, hd = x.read().decode("utf-8", "replace"), x.status, dict(x.headers)
        except urllib.error.HTTPError as e:
            raw, st, hd = e.read().decode("utf-8", "replace"), e.code, dict(e.headers)
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            return Resp(0, {"transport_error": type(e).__name__}, type(e).__name__, time.time() - t)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw
        return Resp(st, body, raw, time.time() - t, hd)


def classify(app: str, status: int, body: Any) -> str:
    text = (body if isinstance(body, str) else json.dumps(body, default=str)).lower() if body is not None else ""
    if status == 0 or status in (502, 503, 504):
        return "unavailable"
    if status == 401:
        return "credential"
    if status == 403:
        return "credential" if "revoked" in text else "permission"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status in (400, 417, 422) and ("duplicate" in text or "already_exist" in text or "already exist" in text):
        return "duplicate"
    if status >= 500:
        return "server"
    return "rejected"


class AppHttp:
    """One app, one credential, one purpose. Instances are never shared between the write path and the verifier."""

    def __init__(self, app: str, base: str, auth: "Callable[[], dict]", transport: Transport, redactor: Redactor = NULL,
                 *, timeout: float = 30.0, read_only: bool = False, role: str = ""):
        self.app, self.base, self._auth, self.transport = app, base.rstrip("/"), auth, transport
        self.redactor, self.timeout, self.read_only, self.role = redactor, timeout, read_only, role

    def request(self, method: str, path: str, body: "dict | None" = None, *, timeout: "float | None" = None) -> Resp:
        if self.read_only and method.upper() != "GET":
            raise ReadOnlyViolation(f"{self.app}: this client is read-only; refusing {method}")
        url = self.base + path.replace(" ", "%20")
        headers = {"Accept": "application/json", **self._auth()}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        resp = self.transport.send(Request(method.upper(), url, headers, data, timeout or self.timeout))
        resp.body = self.redactor.scrub(resp.body)
        resp.raw = self.redactor.text(resp.raw)
        return resp

    def get(self, path: str, **kw) -> Resp:
        return self.request("GET", path, **kw)

    def check(self, resp: Resp, ok: tuple = (200, 201, 202), *, op: str = "") -> Resp:
        if resp.status in ok:
            return resp
        kind = classify(self.app, resp.status, resp.body)
        msg = self.redactor.text((resp.raw or "")[:300])
        raise AppError(self.app, kind, resp.status, msg, op=op)


def auth_for(app: str, creds: dict) -> "Callable[[], dict]":
    """Header factory. ``creds`` maps field -> Secret; the secret is revealed only inside the returned closure."""
    def g(k: str) -> str:
        v = creds[k]
        return v.reveal() if isinstance(v, Secret) else str(v)
    if app == "chatwoot":
        return lambda: {"api_access_token": g("token")}
    if app == "twenty":
        return lambda: {"Authorization": "Bearer " + g("token")}
    if app == "erpnext":
        return lambda: {"Authorization": "token %s:%s" % (g("api_key"), g("api_secret"))}
    if app == "lago":
        return lambda: {"Authorization": "Bearer " + g("api_key")}
    raise KeyError(app)
