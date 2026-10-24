"""Execution authorization: the operator boundary's proof that a TRUSTED control point decided this exact call.

Authentication says who the caller is. It does not say what that caller may do. An operator in production therefore
requires, for every side-effecting invocation, either a human approval token (approval.py) or an execution
authorization from this module, issued only AFTER the owner's envelope and policy were evaluated (the mission runtime's
authority gate, or the direct-action authorizer in action_auth.py).

Properties (all enforced by `verify_execution_authorization`, which never raises and fails closed):
* bound to operator, capability, tenant, mission, node, attempt, lease, envelope digest, idempotency key and the
  canonical effective inputs: change a recipient, an amount or the capability and it no longer verifies;
* the mission identity comes from the SIGNED claims, never from a field of the request body;
* short-lived and single-use: the nonce is consumed through a replay guard, which in production must be durable and
  shared (a per-process cache cannot stop a replay on another replica);
* domain-separated from approval tokens: a different derived key, so an authorization can never pass as an approval
  (which would run a handler as if a human had cleared it) nor an approval as an authorization.

A token is minted at most once per reservation: the ledger's `lease_dispatch_once` is the only path to one, so after a
timeout (UNKNOWN) or a restart a fresh token cannot be obtained to retry the effect.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets as _secrets
import time

from .approval import _b64, _norm_id, _secret_bytes, _unb64, canonical, inputs_hash

KIND = "exec_auth/v1"
EXEC_AUTH_KEY = "_exec_auth"                       # reserved input key carrying the claims from runtime to client
DEFAULT_TTL_S = 120


def is_exec_auth_key(k: object) -> bool:
    return isinstance(k, str) and k.lstrip("_").lower().startswith("exec_auth")


def strip_exec_auth_keys(obj):
    """Drop `_exec_auth*` keys recursively: untrusted callers may never smuggle claims in the request body."""
    if isinstance(obj, dict):
        return {k: strip_exec_auth_keys(v) for k, v in obj.items() if not is_exec_auth_key(k)}
    if isinstance(obj, list):
        return [strip_exec_auth_keys(v) for v in obj]
    return obj


def _key(secret: "bytes | str | None") -> bytes:
    base = _secret_bytes(secret)
    if not base:
        return b""
    return hmac.new(base, b"redevops/exec-auth/v1", hashlib.sha256).digest()      # domain separation from approvals


def mint_execution_authorization(secret: "bytes | str | None" = None, *, operator: str, capability: str, tenant: str,
                                 mission_id: str, node_id: str, lease_id: str, attempt: int, envelope_digest: str,
                                 inputs: dict, idempotency_key: str = "", ttl_s: int = DEFAULT_TTL_S,
                                 now: "float | None" = None, dispatch_by: str = "runtime") -> "str | None":
    key = _key(secret)
    if not key or not (tenant and mission_id and lease_id):
        return None                                  # never mint an unbound authorization
    payload = canonical({
        "kind": KIND, "operator": operator, "capability": capability, "tenant": tenant, "mission_id": mission_id,
        "node_id": node_id, "lease_id": lease_id, "attempt": int(attempt), "envelope_digest": envelope_digest,
        "idempotency_key": idempotency_key or "", "dispatch_by": dispatch_by, "inputs_sha256": inputs_hash(inputs),
        "exp": (time.time() if now is None else now) + ttl_s, "nonce": _secrets.token_hex(16)})
    return _b64(payload) + "." + _b64(hmac.new(key, payload, hashlib.sha256).digest())


def verify_execution_authorization(secret: "bytes | str | None" = None, token: object = None, *, operator: str,
                                   capability: str, inputs: dict, now: "float | None" = None,
                                   tenant: "str | None" = None, idempotency_key: str = "",
                                   replay_guard=None) -> "dict | None":
    """Return the signed claims iff the token fully verifies and is consumed, else None. Never raises.
    `tenant` is the AUTHENTICATED caller's tenant (None for an admin credential): a tenant caller can only use an
    authorization issued for that tenant."""
    key = _key(secret)
    if not key or not isinstance(token, str):
        return None
    try:
        p64, s64 = token.split(".")
        payload, sig = _unb64(p64), _unb64(s64)
        if not hmac.compare_digest(hmac.new(key, payload, hashlib.sha256).digest(), sig):
            return None
        d = json.loads(payload)
        t = time.time() if now is None else now
        if not (isinstance(d, dict) and d.get("kind") == KIND and float(d["exp"]) > t):
            return None
        if d["operator"] != operator or d["capability"] != capability:
            return None
        if tenant is not None and d["tenant"] != tenant:
            return None
        if not _norm_id(d.get("mission_id")) or not _norm_id(d.get("lease_id")):
            return None
        if (d.get("idempotency_key") or "") != (idempotency_key or ""):
            return None
        if not hmac.compare_digest(str(d["inputs_sha256"]), inputs_hash(inputs)):
            return None
        if replay_guard is not None and not replay_guard.consume(str(d["nonce"]), float(d["exp"]), now=t):
            return None
        out = {k: d[k] for k in ("tenant", "mission_id", "node_id", "lease_id", "attempt", "envelope_digest",
                                 "capability", "operator", "idempotency_key")}
        out["dispatch_by"] = d.get("dispatch_by", "runtime")
        return out
    except Exception:  # noqa: BLE001 - a malformed token of any shape fails closed
        return None
