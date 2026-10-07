"""Metabase static (signed) embedding — the OSS-compatible way to put a dashboard/question in our own UI.

Metabase's *interactive* embedding + whitelabel are Enterprise-only, but **static embedding** works on the OSS
image: an iframe loads `/embed/{dashboard|question}/{jwt}` where the JWT is HS256-signed with the instance's
embedding secret (`MB_EMBEDDING_SECRET`), so Metabase only renders resources we explicitly signed, with locked
parameters. This module signs that token and builds the URL — no PyJWT dependency (HS256 is hmac+sha256+base64url).

This is what lets the portable Sidekick dock show the actual Metabase dashboard in-context (L2 embed) rather than
bouncing the user to a separate tab.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Mapping


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign_embed_token(secret: str, resource: Mapping[str, int], *, params: Mapping[str, Any] | None = None,
                     ttl_seconds: int = 600, now: int | None = None) -> str:
    """HS256-sign a Metabase embedding JWT. ``resource`` is e.g. ``{"dashboard": 7}`` or ``{"question": 40}``;
    ``params`` locks dashboard/question parameters (an empty dict = all params locked to their defaults)."""
    if not secret:
        raise ValueError("MB_EMBEDDING_SECRET is required to sign an embed token")
    header = {"alg": "HS256", "typ": "JWT"}
    payload: dict[str, Any] = {"resource": dict(resource), "params": dict(params or {})}
    if ttl_seconds:
        payload["exp"] = (now if now is not None else int(time.time())) + ttl_seconds
    signing_input = (_b64u(json.dumps(header, separators=(",", ":")).encode()) + "." +
                     _b64u(json.dumps(payload, separators=(",", ":")).encode()))
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64u(sig)


def embed_url(base_url: str, kind: str, resource_id: int, secret: str, *, params: Mapping[str, Any] | None = None,
              ttl_seconds: int = 600, bordered: bool = True, titled: bool = True, now: int | None = None) -> str:
    """Full signed embed URL for a dashboard or question, ready to drop into an iframe ``src``."""
    if kind not in ("dashboard", "question"):
        raise ValueError(f"kind must be 'dashboard' or 'question', not {kind!r}")
    token = sign_embed_token(secret, {kind: resource_id}, params=params, ttl_seconds=ttl_seconds, now=now)
    frag = f"#bordered={'true' if bordered else 'false'}&titled={'true' if titled else 'false'}"
    return f"{base_url.rstrip('/')}/embed/{kind}/{token}{frag}"


def decode_embed_token(secret: str, token: str) -> dict:
    """Verify + decode a token we signed (for tests / debugging). Raises on a bad signature."""
    signing_input, _, sig_b64 = token.rpartition(".")
    expected = _b64u(hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig_b64):
        raise ValueError("bad embed-token signature")
    payload_b64 = signing_input.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))


__all__ = ["sign_embed_token", "embed_url", "decode_embed_token"]
