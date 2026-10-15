"""Short-lived, signed, document-scoped content URLs: how an external editor fetches the bytes we decided to send.

The editor cannot authenticate to the connector, so access is by an HMAC-signed capability token that binds
(kind, tenant, doc id, version, sha256, backend, expiry). Tokens live at most ``MAX_TTL_S`` (5 minutes). The token is
the only secret in the URL (unavoidable: the editor must fetch it); it never grants anything but those exact bytes,
and the bytes are re-hashed against the bound sha256 on every read. The signing key is random per process unless
``DOCEDITOR_CONTENT_SIGNING_SECRET`` is set (needed when more than one replica serves content).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Optional

from .errors import DocEditorError, NotFound

ENV_SECRET = "DOCEDITOR_CONTENT_SIGNING_SECRET"
MAX_TTL_S = 300


class ContentTokenError(DocEditorError):
    code, status, http_status = "invalid_content_token", "forbidden", 403


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class ContentRegistry:
    def __init__(self, base_url: str, secret: "str | bytes | None" = None):
        self.base_url = base_url.rstrip("/")
        env = os.environ.get(ENV_SECRET, "")
        s = secret if secret is not None else (env or secrets.token_hex(32))
        self._key = s.encode() if isinstance(s, str) else s
        self._inline: dict = {}
        self._lock = threading.Lock()

    def _sign(self, payload: dict) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return _b64(raw) + "." + _b64(hmac.new(self._key, raw, hashlib.sha256).digest())

    def issue(self, *, kind: str, tenant: str, doc_id: str = "", version: int = 0, sha256: str, backend: str,
              ttl_s: int = MAX_TTL_S, inline: Optional[bytes] = None, media_type: str = "application/octet-stream",
              now: Optional[float] = None) -> str:
        ttl = max(1, min(int(ttl_s), MAX_TTL_S))
        t = time.time() if now is None else now
        payload = {"k": kind, "t": tenant, "d": doc_id, "v": version, "h": sha256, "b": backend, "m": media_type,
                   "exp": t + ttl, "n": secrets.token_hex(8)}
        if inline is not None:
            if hashlib.sha256(inline).hexdigest() != sha256:
                raise ContentTokenError("inline content does not match its bound hash")
            with self._lock:
                self._gc(t)
                self._inline[sha256] = (inline, t + ttl)
        return f"{self.base_url}/content/{self._sign(payload)}"

    def _gc(self, now: float) -> None:
        for k in [k for k, (_, e) in self._inline.items() if e < now]:
            self._inline.pop(k, None)

    def verify(self, token: str, *, now: Optional[float] = None) -> dict:
        try:
            p64, s64 = token.split(".")
            raw = _unb64(p64)
            if not hmac.compare_digest(hmac.new(self._key, raw, hashlib.sha256).digest(), _unb64(s64)):
                raise ContentTokenError("bad signature")
            payload = json.loads(raw)
        except ContentTokenError:
            raise
        except Exception:  # noqa: BLE001
            raise ContentTokenError("malformed token") from None
        if float(payload.get("exp", 0)) < (time.time() if now is None else now):
            raise ContentTokenError("token expired")
        return payload

    def inline_bytes(self, sha256: str) -> bytes:
        with self._lock:
            hit = self._inline.get(sha256)
        if not hit or hit[1] < time.time():
            raise NotFound("content expired")
        return hit[0]
