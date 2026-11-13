"""An emulated RAP runtime peer, written strictly from the RAP specification (hydro-project/infinity docs/rap/spec at the pinned commit).

It is NOT Infinity. It plays the runtime role: it keeps no per-call process (a call is a row of data), POSTs invocations, receives results on a
callback URL it generated (an unguessable single-use path token, which the specification allows), and applies the receiver rules the
specification states: validate the schema, route by ``group_id`` and ``id``, never inject an unmatched result, process duplicates once.
On top of the specification it verifies the bridge's ``RAP-HMAC-SHA256-V1`` callback signature with the reference verifier.
"""
from __future__ import annotations

import http.client
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..callback_auth import AuthError, CallbackVerifier, sign_request
from ..contracts import PATH_CANCEL, PATH_CLOSE, PATH_INVOKE, RAP_TOOLSET_PATH


class EmulatedRuntime:
    def __init__(self, bridge_url: str, key_id: str, secret: bytes, audience: str, tenant: str, *, clock: Callable[[], float] = time.time):
        u = urllib.parse.urlsplit(bridge_url)
        self.host, self.port = u.hostname, u.port
        self.key_id, self.secret, self.audience, self.tenant, self.clock = key_id, secret, audience, tenant, clock
        self.pending: Dict[Tuple[str, str], dict] = {}        # (group_id, call id) -> {"token", "operation", "subscription"}
        self.tokens: Dict[str, Tuple[str, str]] = {}
        self.results: Dict[Tuple[str, str], List[dict]] = {}
        self.events: List[dict] = []
        self.wakes: List[Tuple[str, str, str]] = []           # (group, id, type) each time a message woke a hibernated thread
        self.rejected: List[Tuple[int, str]] = []
        self.cond = threading.Condition()
        self.drop_next_ack = 0                                # reply 503 to the next N deliveries (a runtime that is down)
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *a): return                  # noqa: E704
            def do_POST(self):                                 # noqa: N802
                n = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(n)
                code = outer.receive(self.path, dict(self.headers.items()), body)
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.cb_port = self.httpd.server_address[1]
        self.cb_origin = f"http://127.0.0.1:{self.cb_port}"
        # a callback's audience is the receiver's own origin
        self.verifier = CallbackVerifier(secret, key_id, self.cb_origin, expect_tenant=tenant, clock=clock)
        threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.05), daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # ── runtime -> tool ───────────────────────────────────────────────────────────────────────────────────────
    def _post(self, path: str, obj: dict, *, method: str = "POST", sign_with: Optional[bytes] = None, headers: Optional[dict] = None) -> int:
        body = json.dumps(obj).encode() if obj is not None else b""
        h = headers if headers is not None else sign_request(sign_with or self.secret, self.key_id, self.audience, method, path, body, ts=int(self.clock()))
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers={"Content-Type": "application/json", **h})
            r = conn.getresponse()
            r.read()
            return r.status
        finally:
            conn.close()

    def discover(self) -> Tuple[int, dict]:
        h = sign_request(self.secret, self.key_id, self.audience, "GET", RAP_TOOLSET_PATH, b"", ts=int(self.clock()))
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request("GET", RAP_TOOLSET_PATH, headers=h)
            r = conn.getresponse()
            data = r.read()
            return r.status, (json.loads(data) if data and r.status == 200 else {})
        finally:
            conn.close()

    def invoke(self, operation: str, arguments: dict, *, call_id: Optional[str] = None, group_id: str = "thread-root", ancestors: Tuple[str, ...] = (),
               user_id: Optional[str] = None, callback_url: Optional[str] = None, raw: Optional[dict] = None) -> Tuple[int, str]:
        cid = call_id or "call_" + secrets.token_hex(6)
        token = secrets.token_urlsafe(24)
        msg = raw if raw is not None else {"operation": operation, "arguments": arguments, "id": cid, "call_id": None,
                                           "callback_url": callback_url or f"{self.cb_origin}/cb/{token}", "group_id": group_id}
        if raw is None and ancestors:
            msg["thread_ancestors"] = list(ancestors)
        if raw is None and user_id:
            msg["user_id"] = user_id
        with self.cond:
            self.pending.setdefault((group_id, cid), {"token": token, "operation": operation, "subscription": False})
            self.tokens[token] = (group_id, cid)
        return self._post(PATH_INVOKE, msg), cid

    def cancel(self, group_id: str, call_id: str) -> int:
        return self._post(PATH_CANCEL, {"thread_id": group_id, "tool_call_id": call_id})

    def close_thread(self, group_id: str) -> int:
        return self._post(PATH_CLOSE, {"thread_id": group_id})

    # ── tool -> runtime ───────────────────────────────────────────────────────────────────────────────────────
    def receive(self, path: str, headers: dict, body: bytes) -> int:
        if self.drop_next_ack > 0:
            self.drop_next_ack -= 1
            return 503
        token = path.rsplit("/", 1)[-1]
        with self.cond:
            ident = self.tokens.get(token)
        if ident is None:
            self.rejected.append((404, "unknown_token"))
            return 404
        try:
            verdict, claims = self.verifier.verify(path, headers, body)
        except AuthError as e:
            self.rejected.append((e.status, e.reason))
            return e.status
        try:
            msg = json.loads(body)
        except ValueError:
            return 400
        if verdict == "duplicate":
            return 200                                         # acknowledged, not applied twice
        gid, cid = ident
        if claims["group"] != gid or claims["call"] != cid or msg.get("group_id") != gid:
            self.rejected.append((404, "unmatched"))
            return 404                                         # never inject an unmatched message
        typ = msg.get("type")
        with self.cond:
            if typ == "tool_result" and msg.get("id") == cid:
                self.results.setdefault((gid, cid), []).append(msg)
                if msg.get("subscription"):
                    self.pending[(gid, cid)]["subscription"] = True
                self.wakes.append((gid, cid, typ))
            elif typ == "subscription_event" and msg.get("tool_call_id") == cid and self.pending[(gid, cid)]["subscription"]:
                self.events.append(msg)
                self.wakes.append((gid, cid, typ))
                if msg.get("final"):
                    self.pending[(gid, cid)]["subscription"] = False
            else:
                self.rejected.append((400, "unexpected_type"))
                return 400
            self.cond.notify_all()
        return 200

    def await_result(self, group_id: str, call_id: str, timeout: float = 10.0) -> Optional[dict]:
        end = time.time() + timeout
        with self.cond:
            while not self.results.get((group_id, call_id)):
                left = end - time.time()
                if left <= 0:
                    return None
                self.cond.wait(left)
            return self.results[(group_id, call_id)][0]

    def await_events(self, n: int, timeout: float = 10.0) -> List[dict]:
        end = time.time() + timeout
        with self.cond:
            while len(self.events) < n:
                left = end - time.time()
                if left <= 0:
                    break
                self.cond.wait(left)
            return list(self.events)
