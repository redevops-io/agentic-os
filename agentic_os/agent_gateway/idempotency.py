"""Idempotency for the gateway: what a key is bound to, and the bounded store that remembers it.

A client-chosen idempotency key makes a retry safe: the second call with the same key returns the stored result and the effect is not
executed again. This module defines the two things that make that safe to serve:

* the **fingerprint** a key is bound to (:func:`fingerprint`): a SHA-256 over a canonical form of the execution scope (tenant, workspace,
  subject, the resolved authority key, the capability) and the action's arguments. A key reused with a different fingerprint is a
  conflict, never a replay;
* the **store** (:class:`InMemoryIdempotencyStore`): an atomic claim per key (so concurrent retries execute once), a time-to-live and a
  size bound. An in-flight claim is never evicted.

Canonical form of the arguments (stable across processes and releases of this module): ``idempotency_key`` is dropped (it is the key, not
an input); strings are NFC-normalised; an integral float becomes an int (``1.0`` and ``1`` are the same argument; ``True`` stays a bool);
mapping keys are sorted and must be strings; tuples and lists are the same sequence; anything else that JSON cannot carry is rejected
rather than stringified. The result is JSON with sorted keys and no insignificant whitespace, bounded by ``MAX_CANONICAL_BYTES`` and
``MAX_DEPTH``; a larger or deeper argument set cannot be bound to a key and the call fails closed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Tuple

MAX_KEY_CHARS = 256
MAX_DEPTH = 16
MAX_CANONICAL_BYTES = 256 * 1024
DEFAULT_TTL_S = 24 * 3600.0
DEFAULT_MAX_ENTRIES = 10_000

NEW, REPLAY, CONFLICT, IN_PROGRESS = "new", "replay", "conflict", "in_progress"


class IdempotencyError(ValueError):
    """The key or the arguments cannot be bound (too long, too large, too deep, not JSON). The gateway fails the call closed."""


class IdempotencyStoreFull(RuntimeError):
    """Every slot holds an in-flight claim, so nothing can be evicted to make room."""


def _normal(value: Any, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise IdempotencyError("arguments are nested too deeply to bind to an idempotency key")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise IdempotencyError("a non-finite number cannot be bound to an idempotency key")
        return int(value) if value.is_integer() else value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise IdempotencyError("argument names must be strings")
            out[unicodedata.normalize("NFC", k)] = _normal(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_normal(v, depth + 1) for v in value]
    raise IdempotencyError(f"a {type(value).__name__} argument cannot be bound to an idempotency key")


def canonical_arguments(arguments: Mapping[str, Any]) -> str:
    """The canonical JSON of the arguments that identify an action (see the module docstring)."""
    body = {k: v for k, v in dict(arguments or {}).items() if k != "idempotency_key"}
    text = json.dumps(_normal(body, 0), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if len(text) > MAX_CANONICAL_BYTES:
        raise IdempotencyError("arguments are too large to bind to an idempotency key")
    return text


def normalise_key(key: Any) -> str:
    text = unicodedata.normalize("NFC", str(key))
    if not text or len(text) > MAX_KEY_CHARS:
        raise IdempotencyError(f"an idempotency key must be 1..{MAX_KEY_CHARS} characters")
    return text


def fingerprint(*, tenant: str, workspace: str, subject: str, authority_key: str, capability: str, arguments: Mapping[str, Any]) -> str:
    """What an idempotency key is bound to. Two requests with the same key replay each other only if this is equal."""
    scope = json.dumps([tenant, workspace, subject, authority_key, capability], separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256((scope + "\n" + canonical_arguments(arguments)).encode("utf-8")).hexdigest()


class IdempotencyStore(Protocol):
    """Remembers the result of an idempotent call under (tenant, key), bound to a fingerprint.

    ``begin`` is atomic: of any number of concurrent callers presenting the same (tenant, key), exactly one gets ``NEW`` (it holds the claim and
    will execute); the others get ``IN_PROGRESS``, or ``REPLAY`` once the claim is completed, or ``CONFLICT`` if their fingerprint differs.
    A claim ends with ``complete`` (the result is stored) or ``abandon`` (nothing is stored; a retry may claim again). ``seed`` records a
    result that an operator established by reconciliation."""

    def begin(self, tenant: str, key: str, fingerprint: str) -> Tuple[str, Any]: ...
    def complete(self, tenant: str, key: str, fingerprint: str, result: Any) -> None: ...
    def abandon(self, tenant: str, key: str, fingerprint: str) -> None: ...
    def seed(self, tenant: str, key: str, fingerprint: str, result: Any) -> None: ...


@dataclass
class _Entry:
    fingerprint: str
    result: Any = None
    done: bool = False
    expires_at: float = 0.0


@dataclass
class InMemoryIdempotencyStore:
    """Process-local store: a time-to-live (``ttl_s``) and a bound on the number of entries (``max_entries``). When full, the oldest *completed*
    entries are evicted first; an in-flight claim is never evicted, and if nothing can be evicted a new claim is refused
    (:class:`IdempotencyStoreFull`) rather than the bound exceeded. After its TTL or eviction a key is simply unknown: a retry executes again,
    which is why an unknown outcome is held by the authority ledger (``outcome_unknown``) and not here. Not shared between processes."""

    ttl_s: float = DEFAULT_TTL_S
    max_entries: int = DEFAULT_MAX_ENTRIES
    clock: Callable[[], float] = time.time
    _entries: "OrderedDict[Tuple[str, str], _Entry]" = field(default_factory=OrderedDict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if self.ttl_s <= 0 or self.max_entries < 1:
            raise ValueError("ttl_s must be positive and max_entries at least 1")

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _live(self, ident: Tuple[str, str], now: float) -> Optional[_Entry]:
        e = self._entries.get(ident)
        if e is not None and e.done and now >= e.expires_at:
            del self._entries[ident]                              # expired: forgotten (an in-flight claim has no expiry)
            return None
        return e

    def _make_room(self, now: float) -> None:
        if len(self._entries) < self.max_entries:
            return
        for ident in [i for i, e in self._entries.items() if e.done and now >= e.expires_at]:
            del self._entries[ident]
        for ident in [i for i, e in self._entries.items() if e.done]:         # oldest completed first (insertion order)
            if len(self._entries) < self.max_entries:
                return
            del self._entries[ident]
        if len(self._entries) >= self.max_entries:
            raise IdempotencyStoreFull("the idempotency store is full of in-flight calls")

    def begin(self, tenant: str, key: str, fingerprint: str) -> Tuple[str, Any]:
        with self._lock:
            now, ident = self.clock(), (tenant, key)
            e = self._live(ident, now)
            if e is None:
                self._make_room(now)
                self._entries[ident] = _Entry(fingerprint)
                return NEW, None
            if e.fingerprint != fingerprint:
                return CONFLICT, None
            if not e.done:
                return IN_PROGRESS, None
            return REPLAY, copy.deepcopy(e.result)

    def complete(self, tenant: str, key: str, fingerprint: str, result: Any) -> None:
        with self._lock:
            e = self._entries.get((tenant, key))
            if e is None or e.done or e.fingerprint != fingerprint:
                return                                                         # not our claim: never overwrite someone else's entry
            e.result, e.done, e.expires_at = copy.deepcopy(result), True, self.clock() + self.ttl_s
            self._entries.move_to_end((tenant, key))                           # eviction order is by completion

    def abandon(self, tenant: str, key: str, fingerprint: str) -> None:
        with self._lock:
            e = self._entries.get((tenant, key))
            if e is not None and not e.done and e.fingerprint == fingerprint:
                del self._entries[(tenant, key)]

    def seed(self, tenant: str, key: str, fingerprint: str, result: Any) -> None:
        with self._lock:
            now, ident = self.clock(), (tenant, key)
            e = self._live(ident, now)
            if e is not None and not e.done:
                return                                                         # a call is in flight under this key: it will store its own result
            if e is None:
                self._make_room(now)
            self._entries.pop(ident, None)
            self._entries[ident] = _Entry(fingerprint, copy.deepcopy(result), True, now + self.ttl_s)
