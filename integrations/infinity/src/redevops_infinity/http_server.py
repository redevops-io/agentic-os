"""The HTTP surface of the RAP bridge (stdlib only). Four endpoints, nothing else:

    GET  /.well-known/rap-toolset   discovery        (scope ``discover``)
    POST /invoke                    tool invocation  (scope ``invoke``)
    POST /cancel_tool_call          best-effort cancel (scope ``cancel``)
    POST /close_thread              best-effort thread closure (scope ``close``)

Deny by default. Every request, whatever the method or path, is authenticated first (``RAP-HMAC-SHA256-V1``: signature, audience, clock
skew, a never-seen nonce) and only then routed; an unauthenticated request learns nothing about which paths exist (always 401). A request
is rate limited per source address before authentication and per peer and endpoint after it. Bodies are capped, must be JSON, and a
chunked body is refused. The listener binds to loopback unless told otherwise. Replies carry no body except a short fixed error code.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

from .callback_auth import AuthError, RateLimiter
from .contracts import MAX_BODY_BYTES, PATH_CANCEL, PATH_CLOSE, PATH_INVOKE, RAP_TOOLSET_PATH
from .rap_gateway import RapGateway

_log = logging.getLogger("redevops_infinity.http")


class RapHttpServer:
    def __init__(self, bridge: RapGateway, *, host: str = "127.0.0.1", port: int = 0, public_base_url: str = "",
                 ip_rate_per_s: float = 20.0, ip_burst: int = 40, socket_timeout_s: float = 10.0, limiter: Optional[RateLimiter] = None):
        self.bridge, self.limiter = bridge, limiter or RateLimiter()
        self.ip_rate, self.ip_burst = ip_rate_per_s, ip_burst
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = socket_timeout_s
            server_version = "rap-bridge"
            sys_version = ""

            def log_message(self, fmt, *args):  # noqa: N802 - no access log with headers or bodies
                return

            def _reply(self, status: int, code: str = "") -> None:
                body = json.dumps({"error": code}).encode() if code and status >= 400 else b""
                self.send_response(status)
                if body:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _handle(self, method: str) -> None:
                status, code = outer.route(method, self.path, dict(self.headers.items()), self.rfile, self.client_address[0], self._json_reply)
                if status is not None:
                    self._reply(status, code)

            def _json_reply(self, status: int, obj: dict) -> None:
                body = json.dumps(obj, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self): self._handle("GET")          # noqa: E704,N802
            def do_POST(self): self._handle("POST")        # noqa: E704,N802
            def do_PUT(self): self._handle("PUT")          # noqa: E704,N802
            def do_DELETE(self): self._handle("DELETE")    # noqa: E704,N802
            def do_PATCH(self): self._handle("PATCH")      # noqa: E704,N802
            def do_HEAD(self): self._handle("HEAD")        # noqa: E704,N802
            def do_OPTIONS(self): self._handle("OPTIONS")  # noqa: E704,N802

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.host, self.port = self.httpd.server_address[0], self.httpd.server_address[1]
        self.base_url = public_base_url or f"http://{self.host}:{self.port}"
        self._t: Optional[threading.Thread] = None

    # The routing function is separate from the socket code so tests can drive it without a socket.
    def route(self, method: str, raw_path: str, headers: dict, rfile, remote: str, json_reply) -> Tuple[Optional[int], str]:
        h = {k.lower(): v for k, v in headers.items()}
        if not self.limiter.allow("ip:" + remote, self.ip_rate, self.ip_burst):
            return 429, "rate_limited"
        path = raw_path.split("?", 1)[0]
        # read the body (bounded) BEFORE authenticating: the signature covers its hash
        if h.get("transfer-encoding"):
            return 411, "length_required"
        try:
            n = int(h.get("content-length", "0") or "0")
        except ValueError:
            return 400, "bad_length"
        if n < 0 or n > MAX_BODY_BYTES:
            return 413, "too_large"
        body = rfile.read(n) if n else b""
        if len(body) != n:
            return 400, "short_body"
        try:
            peer = self.bridge.peers.authenticate(method, raw_path if "?" in raw_path else path, h, body)
        except AuthError as e:
            _log.info("refused: %s", e.reason)
            return e.status, "unauthorized" if e.status == 401 else "forbidden"
        if not self.limiter.allow(f"peer:{peer.key_id}:{path}", peer.rate_per_s, peer.burst):
            return 429, "rate_limited"
        if method == "GET" and path == RAP_TOOLSET_PATH:
            json_reply(200, self.bridge.toolset(peer, self.base_url))
            return None, ""
        if method == "POST" and path in (PATH_INVOKE, PATH_CANCEL, PATH_CLOSE):
            if "application/json" not in h.get("content-type", "").lower():
                return 415, "unsupported_media_type"
            if path == PATH_INVOKE:
                return self.bridge.handle_invoke(peer, body)
            if path == PATH_CANCEL:
                self.bridge.handle_cancel(peer, body)
            else:
                self.bridge.handle_close_thread(peer, body)
            return 200, ""
        return (405 if path in (PATH_INVOKE, PATH_CANCEL, PATH_CLOSE, RAP_TOOLSET_PATH) else 404), "not_found"

    def start(self) -> "RapHttpServer":
        self._t = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.05), name="rap-http", daemon=True)
        self._t.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._t:
            self._t.join(timeout=5)
