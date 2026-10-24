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
import logging
import os
import secrets as _secrets
import threading
import time
from typing import Protocol, runtime_checkable

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


def _norm_id(v: object) -> "str | None":
    """Normalise an id for binding: None/blank => None, else str."""
    if v is None:
        return None
    v = v if isinstance(v, str) else str(v)
    return v if v.strip() else None


def _binding_ok(bound: object, presented: object) -> bool:
    """True iff the signed-payload value ``bound`` is satisfied by the caller-presented value.
    bound null/blank => unbound dimension (minter chose no binding) => anything is accepted.
    bound set => presented must be present and equal (constant time); missing/empty/whitespace => False."""
    b = _norm_id(bound)
    if b is None:
        return True
    p = presented if isinstance(presented, str) else (None if presented is None else str(presented))
    if p is None or p == "":
        return False
    return hmac.compare_digest(b.encode(), p.encode())


def mint_approval_token(secret: "bytes | str | None" = None, *, operator: str, capability: str,
                        mission_id: "str | None", node_id: "str | None", inputs: dict, marker: dict,
                        ttl_s: int = 300, now: "float | None" = None) -> "str | None":
    key = _secret_bytes(secret)
    if not key:
        return None
    payload = canonical({
        "operator": operator, "capability": capability,
        "mission_id": _norm_id(mission_id), "node_id": _norm_id(node_id),
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
        # FAIL CLOSED binding: a dimension the token binds (non-null in the SIGNED payload) must be matched by a
        # presented, non-empty value. A missing/None/empty presented value is a mismatch. Only a dimension the
        # minter left null (unbound) is not checked.
        if not _binding_ok(d.get("mission_id"), mission_id) or not _binding_ok(d.get("node_id"), node_id):
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


@runtime_checkable
class SharedStore(Protocol):
    """SEAM for multi-replica deployments (no implementation shipped). ``ReplayGuard`` and the Operator
    idempotency cache are PER-PROCESS; with N replicas a replayed approval token or a retried idempotency key
    can land on a different replica and be accepted/re-executed. A shared backend (e.g. Redis ``SET key NX EX
    ttl``) implementing this protocol is the intended fix: ``consume`` is an atomic set-if-absent that is True
    only for the first caller; ``get``/``put`` hold idempotency results with a TTL. Until one is wired, run a
    single replica (or sticky-route by operator) and set AGENTIC_OS_REPLICAS so a warning is logged."""

    def consume(self, nonce: str, exp: float, *, now: "float | None" = None) -> bool: ...
    def get(self, key: str) -> "dict | None": ...
    def put(self, key: str, value: dict, ttl: float) -> None: ...


def warn_if_replicated(component: str) -> None:
    """One WARNING when AGENTIC_OS_REPLICAS>1 and a per-process in-memory store is in use."""
    import logging, os  # noqa: PLC0415,E401
    try:
        n = int(os.environ.get("AGENTIC_OS_REPLICAS", "1") or "1")
    except ValueError:
        return
    if n > 1:
        logging.getLogger("agentic_os.operator").warning(
            "AGENTIC_OS_REPLICAS=%d but %s is an in-memory, per-process store: replay protection / "
            "exactly-once is NOT enforced across replicas. Run a single replica or provide a SharedStore "
            "(see agentic_os.mission.approval.SharedStore).", n, component)


class ReplayGuard:
    """Bounded in-memory set of used nonces, each kept until its token expires.

    SEAM: this is per-process. A multi-process / multi-replica operator needs a shared store (e.g. Redis
    ``SET nonce NX EX ttl``) behind the same ``consume(nonce, exp, now=)`` interface."""

    def __init__(self, max_entries: int = 10_000):
        self.max_entries = max_entries
        warn_if_replicated("ReplayGuard (and the Operator idempotency cache)")
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


class SqliteReplayGuard:
    """Durable, cross-process replay guard: the same ``consume(nonce, exp, now=)`` contract as :class:`ReplayGuard`
    over a SQLite file, so a restart, a second worker on the same host, or a crash between consume and dispatch
    cannot let an approval nonce be used twice. ``INSERT`` on a PRIMARY KEY makes consume atomic across
    processes; expired rows are purged opportunistically. Fails closed (False) on any storage error. For
    multi-HOST deployments put the same contract on a shared store (Redis ``SET NX EX``/Postgres)."""

    def __init__(self, path: str, max_entries: int = 100_000):
        import sqlite3  # noqa: PLC0415
        self.path, self.max_entries = path, max_entries
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        # Several processes may open a brand-new file at once: switching to WAL and creating the table take locks the
        # busy timeout does not always cover ("database is locked"), so setup retries instead of crashing the worker.
        self._retry(lambda: self._db.execute("PRAGMA journal_mode=WAL"))
        self._retry(lambda: self._db.execute(
            "CREATE TABLE IF NOT EXISTS used_nonces (nonce TEXT PRIMARY KEY, exp REAL NOT NULL)"))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _retry(fn, *, attempts: int = 60, delay: float = 0.05):
        """Run ``fn``, retrying while SQLite reports the database locked/busy (bounded; the last error is raised)."""
        import sqlite3  # noqa: PLC0415
        for i in range(attempts):
            try:
                return fn()
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if ("locked" not in msg and "busy" not in msg) or i == attempts - 1:
                    raise
                time.sleep(min(delay * (1 + i // 5), 0.5))

    def consume(self, nonce: str, exp: float, *, now: "float | None" = None) -> bool:
        import sqlite3  # noqa: PLC0415
        t = time.time() if now is None else now
        with self._lock:
            try:
                self._retry(lambda: self._db.execute("DELETE FROM used_nonces WHERE exp <= ?", (t,)))
                (n,) = self._retry(lambda: self._db.execute("SELECT COUNT(*) FROM used_nonces").fetchone())
                if n >= self.max_entries:
                    return False
                # a lock must not be mistaken for a replay: retry it; only a PRIMARY KEY conflict means "used"
                self._retry(lambda: self._db.execute("INSERT INTO used_nonces (nonce, exp) VALUES (?, ?)",
                                                     (nonce, float(exp))))
                return True
            except sqlite3.IntegrityError:
                return False                      # already used: a replay
            except sqlite3.Error:
                return False                      # fail closed (storage error that retrying did not clear)

    def close(self) -> None:
        self._db.close()


_WARNED_NON_DURABLE = False
ENV_REPLAY_DB = "AGENTIC_OS_REPLAY_DB"
ENV_REQUIRE_DURABLE_REPLAY = "AGENTIC_OS_REQUIRE_DURABLE_REPLAY"


class ReplayProtectionNotDurable(RuntimeError):
    """Startup refusal: durable replay protection is required but no durable store is configured."""


def default_replay_guard():
    """The guard an Operator uses: durable when ``AGENTIC_OS_REPLAY_DB`` names a file, else per-process.

    PRODUCTION: set ``AGENTIC_OS_REQUIRE_DURABLE_REPLAY=1``. The Operator then REFUSES TO START without a durable
    store, instead of silently falling back to a per-process set that a restart (or a second worker) forgets.
    Without that flag the per-process default is kept for development and demos, and a warning is logged."""
    path = os.environ.get(ENV_REPLAY_DB, "").strip()
    if path:
        return SqliteReplayGuard(path)
    from .profile import is_production  # noqa: PLC0415
    if is_production() or os.environ.get(ENV_REQUIRE_DURABLE_REPLAY, "").strip().lower() in ("1", "true", "yes", "on"):
        raise ReplayProtectionNotDurable(
            f"{ENV_REQUIRE_DURABLE_REPLAY} is set but {ENV_REPLAY_DB} is not: approval replay protection would be "
            "per-process. Set it to a private file on durable storage (or provide a shared store).")
    global _WARNED_NON_DURABLE
    if not _WARNED_NON_DURABLE:           # once per process, on its own logger (not the operator startup logger)
        _WARNED_NON_DURABLE = True
        logging.getLogger("agentic_os.replay_guard").warning(
            "approval replay protection is per-process (%s unset): a restart or a second worker forgets used "
            "approvals. Set %s for production and %s=1 to refuse to start without it.",
            ENV_REPLAY_DB, ENV_REPLAY_DB, ENV_REQUIRE_DURABLE_REPLAY)
    return ReplayGuard()


# ── what an approver is shown about an action (pure; shared with the Go/Kotlin ports by conformance vectors) ──
SUMMARY_KEYS = ("amount", "currency", "total", "quantity", "count", "channel", "subject", "to", "recipient",
                "destination", "invoice_id", "account_id", "customer_id", "order_id", "resource")


def action_summary_of(resolved: dict) -> dict:
    """The concrete facts shown to an approver, from the inputs about to run: an allowlist (``SUMMARY_KEYS``), strings
    truncated to 80 characters with e-mail local parts masked (first one or two characters kept), numbers and booleans
    as they are, null as ``"null"``, anything else as its canonical JSON text truncated to 80. Never free text, never secrets."""
    import re  # noqa: PLC0415
    out: dict = {}
    for k in SUMMARY_KEYS:
        if k not in resolved:
            continue
        v = resolved[k]
        if isinstance(v, str):
            v = re.sub(r"([^@\s]{1,2})[^@\s]*@", r"\1***@", v)[:80]
        elif v is not None and not isinstance(v, (int, float, bool)):
            # canonical JSON, not Python's repr, so every language port renders the same text
            v = json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)[:80]
        elif v is None:
            v = "null"
        out[k] = v
    return out


def approval_expired(parked_at: "float | None", now: float, ttl_s: float) -> bool:
    """A parked gate lapses when ``ttl_s`` > 0 and it has waited longer than that (``ttl_s`` 0 = no limit)."""
    return bool(ttl_s) and parked_at is not None and now - parked_at > ttl_s
