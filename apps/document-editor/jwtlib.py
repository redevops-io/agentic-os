"""Minimal HS256 JSON Web Tokens for ONLYOFFICE Document Server (pure stdlib).

ONLYOFFICE signs/verifies with HS256 and the shared secret configured as ``JWT_SECRET`` (header ``Authorization:
Bearer <jwt>`` by default; optionally the ``token`` field of the body). Verification here is strict: only
``alg=HS256`` is accepted (``none`` and other algorithms are rejected), the signature is compared in constant time,
and ``exp`` / ``nbf`` are enforced when present.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from .errors import DocEditorError


class JWTError(DocEditorError):
    code, status, http_status = "invalid_token", "unauthorized", 401


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign(payload: dict, secret: "str | bytes", *, ttl_s: Optional[int] = None, now: Optional[float] = None) -> str:
    key = secret.encode() if isinstance(secret, str) else secret
    if not key:
        raise JWTError("empty JWT secret")
    body = dict(payload)
    if ttl_s is not None:
        body["exp"] = int((time.time() if now is None else now) + ttl_s)
    head = _b64(_json({"alg": "HS256", "typ": "JWT"}))
    msg = f"{head}.{_b64(_json(body))}"
    return f"{msg}.{_b64(hmac.new(key, msg.encode('ascii'), hashlib.sha256).digest())}"


def verify(token: str, secret: "str | bytes", *, now: Optional[float] = None, leeway_s: int = 0) -> dict:
    key = secret.encode() if isinstance(secret, str) else secret
    if not key or not isinstance(token, str):
        raise JWTError("missing token or secret")
    try:
        h64, p64, s64 = token.split(".")
        header = json.loads(_unb64(h64))
        if header.get("alg") != "HS256":
            raise JWTError("only HS256 is accepted")
        expect = hmac.new(key, f"{h64}.{p64}".encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, _unb64(s64)):
            raise JWTError("bad signature")
        payload = json.loads(_unb64(p64))
    except JWTError:
        raise
    except Exception:  # noqa: BLE001 - any malformed token is simply invalid
        raise JWTError("malformed token") from None
    t = time.time() if now is None else now
    if not isinstance(payload, dict):
        raise JWTError("payload is not an object")
    if "exp" in payload and float(payload["exp"]) + leeway_s < t:
        raise JWTError("token expired")
    if "nbf" in payload and float(payload["nbf"]) - leeway_s > t:
        raise JWTError("token not yet valid")
    return payload
