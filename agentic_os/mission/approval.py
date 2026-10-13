"""Signed approval tokens: carry a human approval to an operator over HTTP without making it forgeable.

The runtime knows a node was approved (``_approval`` marker in the node inputs). In-process that marker is
trusted. Over HTTP the operator's ``/invoke`` must strip every client-supplied ``_approval*`` key (forgery
defence), which also strips the legitimate marker. The fix: the runtime sends an HMAC-signed token in a
separate body field, and the operator re-injects the marker only from a token that verifies.

Token: ``b64url(payload_json) + "." + b64url(HMAC-SHA256(secret, payload_bytes))``. The payload is canonical
JSON (sorted keys, no whitespace) binding: operator, capability, mission_id, node_id, ``inputs_sha256``,
the approval ``marker``, ``exp`` (epoch seconds) and a random ``nonce``.

``inputs_sha256`` is the SHA-256 of the canonical JSON of the inputs after removing runtime-meta keys:
any key whose lowercased name starts with ``_approval`` (or ``__approval``), plus ``_mission``, ``_goal`` and
``_node`` (top level only; nested dicts are hashed as-is). An approval for refund INV-1 $10 therefore cannot
be replayed for INV-2 or $999.

Secret: env ``AGENTIC_OS_APPROVAL_SECRET`` read at call time, else (read-only) the platform secrets file
written by ``agentic-os secrets init`` (docs/SECRETS.md). Unset/empty => mint returns None and verify
always returns None (fail closed: approved execution over HTTP is unavailable, never open).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as _secrets
import threading
import time

ENV_SECRET = "AGENTIC_OS_APPROVAL_SECRET"
_META_KEYS = {"_mission", "_goal", "_node"}


def _file_secret() -> str:
    """Read-only fallback: the platform secrets file (docs/SECRETS.md). Never generates, never raises."""
    try:
        from agentic_os.secrets.bootstrap import read_secrets_file  # noqa: PLC0415
        return read_secrets_file().get(ENV_SECRET, "")
    except Exception:  # noqa: BLE001
        return ""


def _secret_bytes(secret: "bytes | str | None" = None) -> bytes:
    s = (os.environ.get(ENV_SECRET, "") or _file_secret()) if secret is None else secret
    return s.encode() if isinstance(s, str) else (s or b"")


def is_approval_key(k: object) -> bool:
    return isinstance(k, str) and k.lstrip("_").lower().startswith("approval")


def strip_approval_keys(obj):
    """Recursively drop every ``*approval*``-prefixed key (case-insensitive, any leading underscores)."""
    if isinstance(obj, dict):
        return {k: strip_approval_keys(v) for k, v in obj.items() if not is_approval_key(k)}
    if isinstance(obj, list):
        return [strip_approval_keys(v) for v in obj]
    return obj


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def inputs_hash(inputs: dict) -> str:
    clean = {k: v for k, v in (inputs or {}).items() if not is_approval_key(k) and k not in _META_KEYS}
    return hashlib.sha256(canonical(clean)).hexdigest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mint_approval_token(secret: "bytes | str | None" = None, *, operator: str, capability: str,
                        mission_id: "str | None", node_id: "str | None", inputs: dict, marker: dict,
                        ttl_s: int = 300, now: "float | None" = None) -> "str | None":
    key = _secret_bytes(secret)
    if not key:
        return None
    payload = canonical({
        "operator": operator, "capability": capability, "mission_id": mission_id, "node_id": node_id,
        "inputs_sha256": inputs_hash(inputs), "marker": marker,
        "exp": (time.time() if now is None else now) + ttl_s, "nonce": _secrets.token_hex(16)})
    return _b64(payload) + "." + _b64(hmac.new(key, payload, hashlib.sha256).digest())


def verify_approval_token(secret: "bytes | str | None" = None, token: object = None, *, operator: str,
                          capability: str, inputs: dict, now: "float | None" = None,
                          mission_id: "str | None" = None, node_id: "str | None" = None,
                          replay_guard: "ReplayGuard | None" = None) -> "dict | None":
    """Return the approval marker iff the token fully verifies, else None. Never raises."""
    key = _secret_bytes(secret)
    if not key or not isinstance(token, str):
        return None
    try:
        p64, s64 = token.split(".")
        payload, sig = _unb64(p64), _unb64(s64)
        if not hmac.compare_digest(hmac.new(key, payload, hashlib.sha256).digest(), sig):
            return None
        d = json.loads(payload)
        t = time.time() if now is None else now
        if not (isinstance(d, dict) and float(d["exp"]) > t):
            return None
        if d["operator"] != operator or d["capability"] != capability:
            return None
        if mission_id is not None and d.get("mission_id") != mission_id:
            return None
        if node_id is not None and d.get("node_id") != node_id:
            return None
        if not hmac.compare_digest(str(d["inputs_sha256"]), inputs_hash(inputs)):
            return None
        marker = d["marker"]
        if not isinstance(marker, dict):
            return None
        if replay_guard is not None and not replay_guard.consume(str(d["nonce"]), float(d["exp"]), now=t):
            return None
        return marker
    except Exception:  # noqa: BLE001 - malformed token of any shape fails closed
        return None


class ReplayGuard:
    """Bounded in-memory set of used nonces, each kept until its token expires.

    SEAM: this is per-process. A multi-process / multi-replica operator needs a shared store (e.g. Redis
    ``SET nonce NX EX ttl``) behind the same ``consume(nonce, exp, now=)`` interface."""

    def __init__(self, max_entries: int = 10_000):
        self.max_entries = max_entries
        self._used: dict[str, float] = {}
        self._lock = threading.Lock()

    def consume(self, nonce: str, exp: float, *, now: "float | None" = None) -> bool:
        """True the first time a nonce is seen; False on replay. Fails closed (False) when the table is
        full of unexpired nonces."""
        t = time.time() if now is None else now
        with self._lock:
            if len(self._used) >= self.max_entries // 2:
                self._used = {n: e for n, e in self._used.items() if e > t}
            if nonce in self._used:
                return False
            if len(self._used) >= self.max_entries:
                return False
            self._used[nonce] = exp
            return True
