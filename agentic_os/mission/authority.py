"""mission-authority/v1 — the Mission Authority Envelope (MAE).

An agent is free to decide HOW to reach a goal; it is not free to decide WHAT it is authorised to do.
The envelope is the durable, versioned, digest-pinned statement of that authority. It is written by the
owner/platform and read by the enforcement point; the governed party (an agent, a child, a model, a
summary) can neither write nor widen it.

What it adds over `mission-policy/v1` (`policy.py`), which stays the approval/deny rule layer:

* typed grants with **resource selectors** and **parameter limits** (who/where/how much),
* per-action and cumulative **amount**, **call count** and **rate** limits (standing grants),
* an **expiry** and a **revocation epoch** (revoke without restarting work),
* an **autonomy budget** (usd, tokens, wall-clock, actions, children, depth, parallelism),
* **monotonic attenuation** for children: a child envelope is always a subset of its parent's.

Decisions are `ALLOW`, `ESCALATE` (the owner may extend the grant: parks as a pending approval) or
`DENY` (out of scope: refused, with a machine-readable amendment the owner can grant). Evaluation is
pure and deterministic; counters live in a `UsageLedger` so limits survive restarts and replicas when a
durable ledger is used. No model is consulted.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from enum import Enum
from fnmatch import fnmatchcase
from hashlib import sha256
from typing import Any, Mapping, Protocol

from .integrity import AnchorFile, GENESIS, IntegrityError, anchor_key_from_env

CONTRACT_VERSION = "mission-authority/v1"
#: Compensation (an undo) is authorised by its own grants: ``compensate:<forward capability>``. A forward grant, even a bare
#: ``*``, does not cover it (see ``AuthorityGuard.check_compensation``).
COMPENSATE_PREFIX = "compensate:"


# ── selector grammar ────────────────────────────────────────────────────────────────────────────────
#: Capability names and resource selectors are fnmatch globs over opaque strings, restricted by this grammar (docs/SECURITY_MODEL.md,
#: "Authority selectors and amounts"). A selector pattern is 1..MAX_SELECTOR_LEN characters, with no control characters, no leading or trailing
#: whitespace, no ``..`` path segment, no ``**`` (write ``*``) and balanced ``[...]`` classes. A value is matched only when it has no
#: control characters (a newline would let ``*`` span two targets), no ``..`` path segment, and no ``,`` or ``;`` unless the pattern
#: itself contains that character (so ``*@customer.com`` cannot admit ``x@evil.io,a@customer.com``). Nothing is canonicalised or decoded.
MAX_SELECTOR_LEN = 512
MAX_VALUE_LEN = 4096
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_SEGMENT_SPLIT = re.compile(r"[\\/]")
_LIST_SEPARATORS = ",;"


class SelectorError(ValueError):
    """A capability pattern or resource selector that is outside the documented grammar."""


def validate_selector(pattern: Any, *, what: str = "selector") -> str:
    """Return ``pattern`` unchanged when it is inside the grammar; raise :class:`SelectorError` otherwise."""
    if not isinstance(pattern, str) or not pattern:
        raise SelectorError(f"{what} must be a non-empty string")
    if len(pattern) > MAX_SELECTOR_LEN:
        raise SelectorError(f"{what} is longer than {MAX_SELECTOR_LEN} characters")
    if _CONTROL.search(pattern):
        raise SelectorError(f"{what} contains a control character")
    if pattern != pattern.strip():
        raise SelectorError(f"{what} has leading or trailing whitespace")
    if "**" in pattern:
        raise SelectorError(f"{what} contains '**' (use a single '*')")
    if any(seg == ".." for seg in _SEGMENT_SPLIT.split(pattern)):
        raise SelectorError(f"{what} contains a '..' path segment")
    i, n = 0, len(pattern)
    while i < n:
        if pattern[i] == "[":
            j = i + 1
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            k = pattern.find("]", j)
            if k < 0:
                raise SelectorError(f"{what} has an unclosed '['")
            i = k
        i += 1
    return pattern


def _safe_value(value: str, pattern: str) -> bool:
    """May ``value`` be matched against ``pattern`` at all? (See the grammar above.)"""
    if len(value) > MAX_VALUE_LEN or _CONTROL.search(value):
        return False
    if any(seg == ".." for seg in _SEGMENT_SPLIT.split(value)):
        return False
    return all(ch not in value or ch in pattern for ch in _LIST_SEPARATORS)


class Lease(str, Enum):
    """Lifecycle of a counter reservation. RESERVED and DISPATCHED/UNKNOWN all count against the limits; only a
    reservation that provably never dispatched (RESERVED) can be released without reconciliation."""
    RESERVED = "reserved"
    DISPATCHED = "dispatched"      # the effect may now exist outside: never released by time alone
    SETTLED = "settled"            # confirmed executed; counters hold the ACTUAL consumption
    RELEASED = "released"          # confirmed not executed; counters returned
    UNKNOWN = "unknown"            # outcome ambiguous: retained until reconciled


_OPEN = (Lease.RESERVED.value, Lease.DISPATCHED.value, Lease.UNKNOWN.value)


class Verdict(str, Enum):
    ALLOW = "allow"
    ESCALATE = "escalate"   # inside the grant's scope but past a limit/tier: the owner may extend it
    DENY = "deny"           # outside the grant's scope: refused


@dataclass(frozen=True)
class Grant:
    """One typed permission. `capability` is an fnmatch pattern. `resources` maps an argument name to the
    patterns it may take (every value of a list argument must match; a missing argument is a deny, never
    an allow). Limits that are `None` are not applied."""
    capability: str
    resources: tuple[tuple[str, tuple[str, ...]], ...] = ()
    max_tier: int = 1                      # highest RiskTier that runs without an approval
    amount_param: str = "amount"
    max_amount: float | None = None        # per action
    max_total_amount: float | None = None  # cumulative over the envelope's life
    max_calls: int | None = None           # cumulative
    rate_calls: int | None = None          # at most this many calls ...
    rate_window_s: float = 3600.0          # ... per this many seconds
    covers_dynamic_risk: bool = False      # owner opt-in: may also stand in for a score-based risk gate (never regulatory)

    def __post_init__(self) -> None:
        validate_selector(self.capability, what="grant capability")
        for key, patterns in self.resources:
            if not isinstance(key, str) or not key or not patterns:
                raise SelectorError("a resource selector needs an argument name and at least one pattern")
            for pat in patterns:
                validate_selector(pat, what=f"resource selector for {key!r}")
        # one canonical number type per field, so a digest is the same before and after a JSON round-trip
        for f, t in (("max_amount", float), ("max_total_amount", float), ("max_calls", int), ("rate_calls", int)):
            v = getattr(self, f)
            object.__setattr__(self, f, None if v is None else t(v))
        object.__setattr__(self, "max_tier", int(self.max_tier))
        object.__setattr__(self, "covers_dynamic_risk", bool(self.covers_dynamic_risk))
        object.__setattr__(self, "rate_window_s", float(self.rate_window_s))

    def covers(self, other: "Grant") -> bool:
        """True if everything `other` could do is something `self` could already do (for attenuation)."""
        if not fnmatchcase(other.capability, self.capability):
            return False
        mine = dict(self.resources)
        theirs = dict(other.resources)
        for key, patterns in mine.items():            # a restricted resource must stay restricted
            if key not in theirs:
                return False
            if not all(any(fnmatchcase(t, p) for p in patterns) for t in theirs[key]):
                return False
        if other.max_tier > self.max_tier or (other.covers_dynamic_risk and not self.covers_dynamic_risk):
            return False
        for a, b in ((self.max_amount, other.max_amount), (self.max_total_amount, other.max_total_amount),
                     (self.max_calls, other.max_calls)):
            if a is not None and (b is None or b > a):
                return False
        if self.rate_calls is not None:
            if other.rate_calls is None or other.rate_window_s != self.rate_window_s or other.rate_calls > self.rate_calls:
                return False
        return True


@dataclass(frozen=True)
class AutonomyBudget:
    """Ceilings for one envelope. A zero count means none granted (fail closed)."""
    usd: float = 0.0
    tokens: int = 0
    wall_clock_s: float = 0.0              # from issue time; 0 = only `expires_at` bounds the time
    max_actions: int = 0
    max_children: int = 0
    max_depth: int = 0                     # how many delegation levels below this envelope
    max_parallel: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "usd", float(self.usd))
        object.__setattr__(self, "wall_clock_s", float(self.wall_clock_s))
        for f in ("tokens", "max_actions", "max_children", "max_depth", "max_parallel"):
            object.__setattr__(self, f, int(getattr(self, f)))


@dataclass(frozen=True)
class AuthorityEnvelope:
    tenant: str
    principal: str
    mission_id: str
    goal: str = ""
    acceptance: tuple[str, ...] = ()       # externally checkable completion criteria (verifier input)
    grants: tuple[Grant, ...] = ()
    forbidden: tuple[str, ...] = ()        # capability patterns that are never allowed; always wins
    budget: AutonomyBudget = field(default_factory=AutonomyBudget)
    may_delegate: bool = False
    required_constraints: tuple[str, ...] = ()   # mission constraints forced onto every delegated mission
    policy_version: str = "1"
    issued_at: float = 0.0
    expires_at: float = 0.0                # 0 = already expired; an envelope must state its lifetime
    revocation_epoch: int = 0
    parent_digest: str = ""                # set on attenuated children
    depth: int = 0
    parent_mission_id: str = ""            # the parent's envelope key, so the chain can be walked and checked live (omitted when empty)

    def __post_init__(self) -> None:
        for f in ("issued_at", "expires_at"):
            object.__setattr__(self, f, float(getattr(self, f)))
        object.__setattr__(self, "revocation_epoch", int(self.revocation_epoch))
        object.__setattr__(self, "depth", int(self.depth))

    # ── identity ────────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d = _jsonable(self)
        if not d.get("parent_mission_id"):
            d.pop("parent_mission_id", None)       # absent for every non-child envelope: stored forms and digests are unchanged
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "AuthorityEnvelope":
        grants = tuple(Grant(
            capability=g["capability"],
            resources=tuple((k, tuple(v)) for k, v in (g.get("resources") or [])),
            max_tier=int(g.get("max_tier", 1)), amount_param=g.get("amount_param", "amount"),
            max_amount=g.get("max_amount"), max_total_amount=g.get("max_total_amount"),
            max_calls=g.get("max_calls"), rate_calls=g.get("rate_calls"),
            rate_window_s=float(g.get("rate_window_s", 3600.0)),
            covers_dynamic_risk=bool(g.get("covers_dynamic_risk", False))) for g in d.get("grants", ()))
        return cls(
            tenant=d["tenant"], principal=d["principal"], mission_id=d["mission_id"], goal=d.get("goal", ""),
            acceptance=tuple(d.get("acceptance", ())), grants=grants, forbidden=tuple(d.get("forbidden", ())),
            budget=AutonomyBudget(**d.get("budget", {})), may_delegate=bool(d.get("may_delegate", False)),
            required_constraints=tuple(d.get("required_constraints", ())),
            policy_version=str(d.get("policy_version", "1")), issued_at=float(d.get("issued_at", 0.0)),
            expires_at=float(d.get("expires_at", 0.0)), revocation_epoch=int(d.get("revocation_epoch", 0)),
            parent_digest=d.get("parent_digest", ""), depth=int(d.get("depth", 0)),
            parent_mission_id=str(d.get("parent_mission_id", "")))

    def digest(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + sha256(canonical.encode()).hexdigest()

    # ── attenuation ─────────────────────────────────────────────────────────────
    def attenuate(self, *, mission_id: str, principal: str, grants: tuple[Grant, ...], budget: AutonomyBudget,
                  goal: str = "", expires_at: float | None = None, may_delegate: bool = False,
                  now: float | None = None) -> "AuthorityEnvelope":
        """A child envelope that is provably a subset of this one, or `AuthorityError`. The child never
        gains a grant, a larger limit, a longer life, more depth, or fewer forbidden patterns."""
        now = time.time() if now is None else now
        if not self.may_delegate:
            raise AuthorityError("delegation_not_allowed", "this envelope does not allow delegation")
        if self.budget.max_depth < 1:      # `max_depth` = delegation levels still allowed BELOW this envelope
            raise AuthorityError("max_depth", "no further delegation depth is allowed")
        for g in grants:
            if not any(p.covers(g) for p in self.grants):
                raise AuthorityError("grant_not_covered", f"grant {g.capability!r} exceeds the parent's authority")
        b, p = budget, self.budget
        if b.usd > p.usd or b.tokens > p.tokens or b.max_actions > p.max_actions or b.max_parallel > p.max_parallel:
            raise AuthorityError("budget_exceeds_parent", "child budget exceeds the parent's")
        if b.max_children > p.max_children or b.max_depth > p.max_depth - 1:
            raise AuthorityError("budget_exceeds_parent", "child delegation allowance exceeds the parent's")
        remaining = max(0.0, self.expires_at - now)
        if b.wall_clock_s > remaining:
            raise AuthorityError("budget_exceeds_parent", "child wall-clock exceeds the parent's remaining time")
        exp = self.expires_at if expires_at is None else min(expires_at, self.expires_at)
        return replace(self, mission_id=mission_id, principal=principal, goal=goal or self.goal, grants=grants,
                       budget=budget, may_delegate=may_delegate and budget.max_depth > 0, issued_at=now, expires_at=exp,
                       parent_digest=self.digest(), depth=self.depth + 1, parent_mission_id=self.mission_id)


class AuthorityError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    rule: str
    reason: str
    amendment: dict | None = None          # machine-readable "what the owner could grant"
    envelope_digest: str = ""
    grant_index: int = -1                  # which grant allowed it (ALLOW only)
    lease_id: str = ""                     # the counter reservation backing an ALLOW (when a lease id was given)

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


@dataclass(frozen=True)
class Action:
    """The concrete thing about to happen, as the enforcement point (not the model) describes it."""
    capability: str
    arguments: Mapping[str, Any]
    risk_tier: int = 0
    side_effecting: bool = False
    cost_usd: float = 0.0
    tokens: int = 0


# ── counters ────────────────────────────────────────────────────────────────────────────────────────
class UsageLedger(Protocol):
    def reserve(self, key: str, deltas: Mapping[str, float], limits: Mapping[str, float | None],
                rate: tuple[str, int, float] | None = None, now: float | None = None) -> str | None:
        """Atomically add `deltas` to the counters under `key` unless any would pass its limit (or the
        rate window is full). Returns None on success, else the name of the counter that refused."""
    def used(self, key: str) -> dict[str, float]: ...
    def release(self, key: str, deltas: Mapping[str, float]) -> None: ...
    # reservation lifecycle (idempotent, atomic): RESERVED -> DISPATCHED -> SETTLED, plus RELEASED and UNKNOWN
    def reserve_lease(self, lease_id: str, key: str, deltas: Mapping[str, float], limits: Mapping[str, float | None],
                      rate: tuple[str, int, float] | None = None, now: float | None = None,
                      meta: Mapping[str, Any] | None = None) -> str | None: ...
    def lease_annotate(self, lease_id: str, **fields: Any) -> bool: ...        # merge identity/evidence fields into the record
    def lease_get(self, lease_id: str) -> dict | None: ...                    # the full durable record
    def lease_state(self, lease_id: str) -> str | None: ...
    def lease_dispatched(self, lease_id: str) -> bool: ...
    def lease_dispatch_once(self, lease_id: str) -> bool: ...     # True only on the RESERVED -> DISPATCHED transition
    def lease_settle(self, lease_id: str, actual: Mapping[str, float] | None = None) -> bool: ...
    def lease_release(self, lease_id: str) -> bool: ...
    def lease_unknown(self, lease_id: str) -> bool: ...
    def lease_reconcile(self, lease_id: str, executed: bool, actual: Mapping[str, float] | None = None) -> bool: ...
    def lease_sweep(self, older_than_s: float, now: float | None = None) -> int: ...
    def leases_outstanding(self, key: str | None = None, detail: bool = False) -> list: ...


class MemoryLedger:
    durable = False

    def __init__(self) -> None:
        self._c: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._w: dict[tuple[str, str], deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def reserve(self, key, deltas, limits, rate=None, now=None):
        now = time.time() if now is None else now
        if (bad := _bad_delta(deltas)) is not None:
            return bad
        with self._lock:
            cur = self._c[key]
            for name, d in deltas.items():
                lim = limits.get(name)
                if lim is not None and not (cur.get(name, 0.0) + d <= lim + 1e-9):   # also refuses NaN
                    return name
            if rate:
                rname, n, win = rate
                q = self._w[(key, rname)]
                while q and now - q[0] >= win:
                    q.popleft()
                if len(q) >= n:
                    return rname
            for name, d in deltas.items():
                cur[name] += d
            if rate:
                self._w[(key, rate[0])].append(now)
            return None

    def used(self, key):
        with self._lock:
            return dict(self._c.get(key, {}))

    def release(self, key, deltas):
        with self._lock:
            for name, d in deltas.items():
                self._c[key][name] = max(0.0, self._c[key][name] - d)

    # ── reservation lifecycle ──────────────────────────────────────────────────────────
    def _leases(self) -> dict:
        if not hasattr(self, "_l"):
            self._l: dict[str, dict] = {}
        return self._l

    def reserve_lease(self, lease_id, key, deltas, limits, rate=None, now=None, meta=None):
        now = time.time() if now is None else now
        if (bad := _bad_delta(deltas)) is not None:
            return bad
        with self._lock:
            row = self._leases().get(lease_id)
            if row and row["state"] != Lease.RELEASED.value:
                return None                                   # idempotent: already counted, never twice
            cur = self._c[key]
            for name, d in deltas.items():
                lim = limits.get(name)
                if lim is not None and not (cur.get(name, 0.0) + d <= lim + 1e-9):
                    return name
            if rate:
                rname, n, win = rate
                q = self._w[(key, rname)]
                while q and now - q[0] >= win:
                    q.popleft()
                if len(q) >= n:
                    return rname
                q.append(now)
            for name, d in deltas.items():
                cur[name] += d
            self._leases()[lease_id] = {"key": key, "deltas": dict(deltas), "state": Lease.RESERVED.value, "updated": now,
                                        "rate": (rate[0], now) if rate else None, "meta": dict(meta or {}), "created": now}
            return None

    def lease_annotate(self, lease_id, **fields):
        with self._lock:
            row = self._leases().get(lease_id)
            if row is None:
                return False
            row["meta"].update({k: v for k, v in fields.items() if v is not None})
            return True

    def lease_get(self, lease_id):
        with self._lock:
            r = self._leases().get(lease_id)
            return None if r is None else {"lease_id": lease_id, "key": r["key"], "state": r["state"],
                                           "deltas": dict(r["deltas"]), "meta": dict(r["meta"]),
                                           "created": r.get("created", r["updated"]), "updated": r["updated"]}

    def lease_state(self, lease_id):
        with self._lock:
            r = self._leases().get(lease_id)
            return r["state"] if r else None

    def _give_back(self, row, actual=None):
        for name, d in row["deltas"].items():
            self._c[row["key"]][name] = max(0.0, self._c[row["key"]][name] - d + (0.0 if actual is None else actual.get(name, d)))
        if actual is None and row["rate"]:
            q = self._w[(row["key"], row["rate"][0])]
            try:
                q.remove(row["rate"][1])
            except ValueError:
                pass

    def _transition(self, lease_id, allowed_from, to, *, actual=None, give_back=False, now=None):
        with self._lock:
            row = self._leases().get(lease_id)
            if row is None:
                return False
            if row["state"] == to:
                return True                                   # idempotent
            if row["state"] not in allowed_from:
                return False
            if give_back:
                self._give_back(row)
            elif actual is not None:
                self._give_back(row, actual)                  # settle: replace the reserved amount with the actual one
            row["state"], row["updated"] = to, (time.time() if now is None else now)
            return True

    def lease_dispatched(self, lease_id):
        return self._transition(lease_id, (Lease.RESERVED.value,), Lease.DISPATCHED.value)

    def lease_dispatch_once(self, lease_id):
        with self._lock:
            row = self._leases().get(lease_id)
            if row is None or row["state"] != Lease.RESERVED.value:
                return False                                  # already dispatched/settled/unknown/released: no new authority
            row["state"], row["updated"] = Lease.DISPATCHED.value, time.time()
            return True

    def lease_settle(self, lease_id, actual=None):
        return self._transition(lease_id, (Lease.DISPATCHED.value, Lease.UNKNOWN.value), Lease.SETTLED.value,
                                actual=dict(actual) if actual is not None else None)

    def lease_release(self, lease_id):
        return self._transition(lease_id, (Lease.RESERVED.value,), Lease.RELEASED.value, give_back=True)

    def lease_unknown(self, lease_id):
        return self._transition(lease_id, (Lease.DISPATCHED.value,), Lease.UNKNOWN.value)

    def lease_reconcile(self, lease_id, executed, actual=None):
        if executed:
            return self._transition(lease_id, (Lease.DISPATCHED.value, Lease.UNKNOWN.value), Lease.SETTLED.value,
                                    actual=dict(actual) if actual is not None else None)
        return self._transition(lease_id, (Lease.DISPATCHED.value, Lease.UNKNOWN.value), Lease.RELEASED.value, give_back=True)

    def lease_sweep(self, older_than_s, now=None):
        now = time.time() if now is None else now
        with self._lock:
            ids = [i for i, r in self._leases().items() if r["state"] == Lease.RESERVED.value and now - r["updated"] >= older_than_s]
        return sum(1 for i in ids if self.lease_release(i))

    def leases_outstanding(self, key=None, detail=False):
        with self._lock:
            rows = [(i, r) for i, r in self._leases().items() if r["state"] in _OPEN and (key is None or r["key"] == key)]
        if detail:
            return sorted((self.lease_get(i) for i, _ in rows), key=lambda d: d["lease_id"])
        return sorted((i, r["state"]) for i, r in rows)


class SqliteLedger:
    """Durable, multi-process counters (one `BEGIN IMMEDIATE` transaction per reserve)."""
    durable = True

    def __init__(self, path: str) -> None:
        self.path = path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS usage (k TEXT, name TEXT, v REAL NOT NULL, PRIMARY KEY (k, name))")
            c.execute("CREATE TABLE IF NOT EXISTS hits (k TEXT, name TEXT, ts REAL NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS hits_idx ON hits (k, name, ts)")
            c.execute("CREATE TABLE IF NOT EXISTS leases (lease_id TEXT PRIMARY KEY, k TEXT NOT NULL, deltas TEXT NOT NULL, "
                      "state TEXT NOT NULL, rate_name TEXT, rate_ts REAL, updated REAL NOT NULL, meta TEXT, created REAL)")
            cols = {r[1] for r in c.execute("PRAGMA table_info(leases)")}
            for col, typ in (("meta", "TEXT"), ("created", "REAL")):      # databases created before lease records existed
                if col not in cols:
                    c.execute(f"ALTER TABLE leases ADD COLUMN {col} {typ}")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def reserve(self, key, deltas, limits, rate=None, now=None):
        now = time.time() if now is None else now
        if (bad := _bad_delta(deltas)) is not None:
            return bad
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            cur = {n: v for n, v in c.execute("SELECT name, v FROM usage WHERE k=?", (key,))}
            for name, d in deltas.items():
                lim = limits.get(name)
                if lim is not None and not (cur.get(name, 0.0) + d <= lim + 1e-9):
                    c.execute("ROLLBACK")
                    return name
            if rate:
                rname, n, win = rate
                c.execute("DELETE FROM hits WHERE k=? AND name=? AND ts<=?", (key, rname, now - win))
                if c.execute("SELECT COUNT(*) FROM hits WHERE k=? AND name=?", (key, rname)).fetchone()[0] >= n:
                    c.execute("ROLLBACK")
                    return rname
                c.execute("INSERT INTO hits (k, name, ts) VALUES (?,?,?)", (key, rname, now))
            for name, d in deltas.items():
                c.execute("INSERT INTO usage (k,name,v) VALUES (?,?,?) ON CONFLICT(k,name) DO UPDATE SET v=v+excluded.v",
                          (key, name, d))
            c.execute("COMMIT")
            return None
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    def used(self, key):
        with self._conn() as c:
            return {n: v for n, v in c.execute("SELECT name, v FROM usage WHERE k=?", (key,))}

    def release(self, key, deltas):
        with self._conn() as c:
            for name, d in deltas.items():
                c.execute("UPDATE usage SET v=MAX(0, v-?) WHERE k=? AND name=?", (d, key, name))

    # ── reservation lifecycle (every transition is one BEGIN IMMEDIATE transaction) ───────
    def _tx(self, fn):
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            out = fn(c)
            c.execute("COMMIT")
            return out
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    def reserve_lease(self, lease_id, key, deltas, limits, rate=None, now=None, meta=None):
        now = time.time() if now is None else now
        if (bad := _bad_delta(deltas)) is not None:
            return bad

        def go(c):
            row = c.execute("SELECT state FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row and row[0] != Lease.RELEASED.value:
                return None
            cur = {n: v for n, v in c.execute("SELECT name, v FROM usage WHERE k=?", (key,))}
            for name, d in deltas.items():
                lim = limits.get(name)
                if lim is not None and not (cur.get(name, 0.0) + d <= lim + 1e-9):
                    return name
            if rate:
                rname, n, win = rate
                c.execute("DELETE FROM hits WHERE k=? AND name=? AND ts<=?", (key, rname, now - win))
                if c.execute("SELECT COUNT(*) FROM hits WHERE k=? AND name=?", (key, rname)).fetchone()[0] >= n:
                    return rname
                c.execute("INSERT INTO hits (k, name, ts) VALUES (?,?,?)", (key, rname, now))
            for name, d in deltas.items():
                c.execute("INSERT INTO usage (k,name,v) VALUES (?,?,?) ON CONFLICT(k,name) DO UPDATE SET v=v+excluded.v", (key, name, d))
            c.execute("INSERT OR REPLACE INTO leases (lease_id,k,deltas,state,rate_name,rate_ts,updated,meta,created) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (lease_id, key, json.dumps(dict(deltas), sort_keys=True), Lease.RESERVED.value,
                       rate[0] if rate else None, now if rate else None, now,
                       json.dumps(dict(meta or {}), sort_keys=True, default=str), now))
            return None
        return self._tx(go)

    def lease_annotate(self, lease_id, **fields):
        def go(c):
            row = c.execute("SELECT meta FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None:
                return False
            meta = json.loads(row[0] or "{}")
            meta.update({k: v for k, v in fields.items() if v is not None})
            c.execute("UPDATE leases SET meta=? WHERE lease_id=?", (json.dumps(meta, sort_keys=True, default=str), lease_id))
            return True
        return self._tx(go)

    def lease_get(self, lease_id):
        with self._conn() as c:
            r = c.execute("SELECT k, state, deltas, meta, created, updated FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
        if r is None:
            return None
        return {"lease_id": lease_id, "key": r[0], "state": r[1], "deltas": json.loads(r[2]), "meta": json.loads(r[3] or "{}"),
                "created": r[4] if r[4] is not None else r[5], "updated": r[5]}

    def lease_state(self, lease_id):
        with self._conn() as c:
            r = c.execute("SELECT state FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
        return r[0] if r else None

    def _move(self, lease_id, allowed_from, to, *, actual=None, give_back=False):
        def go(c):
            row = c.execute("SELECT k, deltas, state, rate_name, rate_ts FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None:
                return False
            k, deltas, state, rname, rts = row
            if state == to:
                return True
            if state not in allowed_from:
                return False
            d = json.loads(deltas)
            if give_back:
                for n, v in d.items():
                    c.execute("UPDATE usage SET v=MAX(0, v-?) WHERE k=? AND name=?", (v, k, n))
                if rname:
                    c.execute("DELETE FROM hits WHERE rowid IN (SELECT rowid FROM hits WHERE k=? AND name=? AND ts=? LIMIT 1)",
                              (k, rname, rts))
            elif actual is not None:
                for n, v in d.items():
                    c.execute("UPDATE usage SET v=MAX(0, v-?+?) WHERE k=? AND name=?", (v, actual.get(n, v), k, n))
            c.execute("UPDATE leases SET state=?, updated=? WHERE lease_id=?", (to, time.time(), lease_id))
            return True
        return self._tx(go)

    def lease_dispatched(self, lease_id):
        return self._move(lease_id, (Lease.RESERVED.value,), Lease.DISPATCHED.value)

    def lease_dispatch_once(self, lease_id):
        def go(c):
            cur = c.execute("UPDATE leases SET state=?, updated=? WHERE lease_id=? AND state=?",
                            (Lease.DISPATCHED.value, time.time(), lease_id, Lease.RESERVED.value))
            return cur.rowcount == 1                           # exactly one caller (process, replica) wins the transition
        return self._tx(go)

    def lease_settle(self, lease_id, actual=None):
        return self._move(lease_id, (Lease.DISPATCHED.value, Lease.UNKNOWN.value), Lease.SETTLED.value,
                          actual=dict(actual) if actual is not None else None)

    def lease_release(self, lease_id):
        return self._move(lease_id, (Lease.RESERVED.value,), Lease.RELEASED.value, give_back=True)

    def lease_unknown(self, lease_id):
        return self._move(lease_id, (Lease.DISPATCHED.value,), Lease.UNKNOWN.value)

    def lease_reconcile(self, lease_id, executed, actual=None):
        if executed:
            return self._move(lease_id, (Lease.DISPATCHED.value, Lease.UNKNOWN.value), Lease.SETTLED.value,
                              actual=dict(actual) if actual is not None else None)
        return self._move(lease_id, (Lease.DISPATCHED.value, Lease.UNKNOWN.value), Lease.RELEASED.value, give_back=True)

    def lease_sweep(self, older_than_s, now=None):
        now = time.time() if now is None else now
        with self._conn() as c:
            ids = [r[0] for r in c.execute("SELECT lease_id FROM leases WHERE state=? AND ?-updated>=?",
                                           (Lease.RESERVED.value, now, older_than_s))]
        return sum(1 for i in ids if self.lease_release(i))

    def leases_outstanding(self, key=None, detail=False):
        q, a = "SELECT lease_id, state FROM leases WHERE state IN (?,?,?)", list(_OPEN)
        if key is not None:
            q, a = q + " AND k=?", a + [key]
        with self._conn() as c:
            rows = sorted((r[0], r[1]) for r in c.execute(q, a))
        return [self.lease_get(i) for i, _ in rows] if detail else rows


# ── trusted storage ─────────────────────────────────────────────────────────────────────────────────
class EnvelopeStore(Protocol):
    def get(self, mission_id: str) -> AuthorityEnvelope | None: ...
    def put(self, env: AuthorityEnvelope) -> None: ...
    def epoch(self, tenant: str) -> int: ...
    def revoke_all(self, tenant: str) -> int: ...
    def revoke(self, mission_id: str) -> None: ...
    def is_revoked(self, mission_id: str) -> bool: ...


class MemoryEnvelopeStore:
    """Authoritative store. Only owner/platform code calls `put`/`revoke_all`; the gateway only calls `get`."""
    durable = False

    def __init__(self) -> None:
        self._e: dict[str, AuthorityEnvelope] = {}
        self._epoch: dict[str, int] = defaultdict(int)
        self._revoked: set[str] = set()

    def get(self, mission_id):
        return self._e.get(mission_id)

    def put(self, env):
        self._e[env.mission_id] = env

    def epoch(self, tenant):
        return self._epoch[tenant]

    def revoke_all(self, tenant):
        self._epoch[tenant] += 1
        return self._epoch[tenant]

    def revoke(self, mission_id):
        self._revoked.add(mission_id)

    def is_revoked(self, mission_id):
        return mission_id in self._revoked


class AuthorityIntegrityError(IntegrityError):
    """The durable authority store disagrees with its own hash chain or anchor (a grant edited or removed, a revocation deleted, the
    chain cut or rewritten). Decisions fail closed: the guard answers DENY ``authority_integrity``."""


def _rec_hash(prev: str, seq: int, kind: str, key: str, body: str) -> str:
    return "sha256:" + sha256(f"{prev}|{seq}|{kind}|{key}|{body}".encode("utf-8")).hexdigest()


class SqliteEnvelopeStore:
    """Durable authority: survives restarts and is shared by replicas on the same database file. Every `put` is a new immutable
    version (history is kept for audit); `get` returns the latest. Revocation is durable and takes effect on the next decision of any
    process.

    Integrity. Every put, revoke and revoke_all is also appended to a hash-chained ``authority_log`` IN THE SAME TRANSACTION as the
    state it changes, and the chain head is kept in a separate anchor file (``<db>.chain``). The whole chain, and the state tables
    against it (each envelope version's body and digest, the revoked set, each tenant's epoch), are verified when the store is
    opened (``AuthorityIntegrityError`` otherwise); ``get`` / ``is_revoked`` / ``epoch`` run an incremental version before they
    answer: the verified tail is still there unchanged, new records chain, the anchor is not ahead of the log, and the one thing being
    asked about agrees with the log. A database from before the log existed is adopted once (trust on first use) and protected from
    then on. Not covered: a writer who can rewrite the database and the anchor file consistently (mission/integrity.py)."""
    durable = True

    def __init__(self, path: str, *, verify: bool = True, anchor: bool = True, anchor_key: "bytes | None" = None) -> None:
        self.path = path
        self._anchor = AnchorFile(path + ".chain", anchor_key if anchor_key is not None else anchor_key_from_env()) if anchor else None
        self._seen: "tuple[int, str] | None" = None          # (seq, hash) of the last record this process verified
        self._tlock = threading.Lock()
        self._warned = False
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS envelopes (mission_id TEXT, version INTEGER, digest TEXT NOT NULL, "
                      "body TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (mission_id, version))")
            c.execute("CREATE TABLE IF NOT EXISTS epochs (tenant TEXT PRIMARY KEY, epoch INTEGER NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS revoked (mission_id TEXT PRIMARY KEY, at REAL NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS authority_log (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, "
                      "body TEXT NOT NULL, prev TEXT NOT NULL, hash TEXT NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS authority_log_kind_key ON authority_log (kind, key, seq)")
        self._adopt_legacy()
        if verify:
            self.verify()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # ── the log ────────────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _append(c, kind: str, key: str, body: dict) -> "tuple[int, str]":
        """Append one record inside the caller's transaction."""
        row = c.execute("SELECT seq, hash FROM authority_log ORDER BY seq DESC LIMIT 1").fetchone()
        prev_seq, prev = row if row else (0, GENESIS)
        seq = prev_seq + 1
        b = json.dumps(body, sort_keys=True, separators=(",", ":"))
        h = _rec_hash(prev, seq, kind, key, b)
        c.execute("INSERT INTO authority_log (seq, kind, key, body, prev, hash) VALUES (?,?,?,?,?,?)", (seq, kind, key, b, prev, h))
        return seq, h

    def _publish(self, seq: int, h: str) -> None:
        """After COMMIT: the anchor says how far the chain has got. A lagging anchor is harmless (a concurrent writer may have moved
        on); one that cannot be written is reported once and leaves the chain itself intact."""
        with self._tlock:
            if self._seen is None or self._seen[0] < seq:
                self._seen = (seq, h)
        if self._anchor is None:
            return
        try:
            self._anchor.write({"count": seq, "last_seq": seq, "head": h, "updated": time.time()}, fsync=_fsync_on())
        except OSError as e:
            if not self._warned:
                self._warned = True
                import logging  # noqa: PLC0415
                logging.getLogger(__name__).warning("authority anchor %s could not be written (%s): truncation of %s is not detectable",
                                                    self._anchor.path, e, self.path)

    def _tx(self, fn):
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            out = fn(c)
            c.execute("COMMIT")
            return out
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    def _adopt_legacy(self) -> None:
        """A database written before the log existed: record what it holds, once, so it is covered from now on. If an anchor already
        vouches for records the log no longer has, that is not a legacy database: it is a cut chain, and it is refused."""
        def go(c):
            if c.execute("SELECT COUNT(*) FROM authority_log").fetchone()[0]:
                return None
            try:
                rec = self._anchor.read() if self._anchor is not None else None
            except IntegrityError as e:
                raise AuthorityIntegrityError(str(e)) from None
            if rec is not None and rec["count"] > 0:
                raise AuthorityIntegrityError(f"{self.path}: the authority log is empty but its anchor vouches for {rec['count']} record(s): truncated")
            last = None
            for mid, ver, digest, body, created in c.execute("SELECT mission_id, version, digest, body, created FROM envelopes ORDER BY mission_id, version").fetchall():
                try:
                    real = AuthorityEnvelope.from_dict(json.loads(body)).digest()
                except Exception:  # noqa: BLE001
                    real = ""
                if real != digest:
                    raise AuthorityIntegrityError(f"{self.path}: envelope {mid} v{ver} does not match its recorded digest (cannot adopt a tampered database)")
                last = self._append(c, "put", mid, {"version": ver, "digest": digest, "body_sha256": sha256(body.encode()).hexdigest(),
                                                    "at": created, "adopted": True})
            for (mid, at) in c.execute("SELECT mission_id, at FROM revoked ORDER BY mission_id").fetchall():
                last = self._append(c, "revoke", mid, {"at": at, "adopted": True})
            for (tenant, ep) in c.execute("SELECT tenant, epoch FROM epochs ORDER BY tenant").fetchall():
                for i in range(int(ep)):
                    last = self._append(c, "revoke_all", tenant, {"epoch": i + 1, "adopted": True})
            return last
        last = self._tx(go)
        if last is not None:
            self._publish(*last)

    def _walk(self, rows, prev: str, expect: int):
        """Check that `rows` (consecutive, ascending) continue a chain whose last hash is `prev` and whose next seq is `expect`."""
        for seq, kind, key, body, p, h in rows:
            if seq != expect:
                raise AuthorityIntegrityError(f"{self.path}: the authority log has a gap or is reordered at record {expect} (found {seq})")
            if p != prev or _rec_hash(prev, seq, kind, key, body) != h:
                raise AuthorityIntegrityError(f"{self.path}: the authority log's hash chain is broken at record {seq}")
            prev, expect = h, expect + 1
        return prev, expect

    def _anchor_rec(self) -> "dict | None":
        """The anchor record. Read BEFORE the log is: a writer commits the record first and moves the anchor second, so an anchor read
        first is never ahead of a log read afterwards (read in the other order, a concurrent commit looks like truncation)."""
        if self._anchor is None:
            return None
        try:
            return self._anchor.read()
        except IntegrityError as e:
            raise AuthorityIntegrityError(str(e)) from None

    def _check_anchor(self, c, n: int, rec: "dict | None") -> None:
        if rec is None or rec["count"] == 0:
            return
        k = rec["count"]
        if n < k:
            raise AuthorityIntegrityError(f"{self.path}: the authority log was truncated: it has {n} record(s) and its anchor vouches for {k}")
        row = c.execute("SELECT hash FROM authority_log WHERE seq=?", (k,)).fetchone()
        if row is None or row[0] != rec["head"]:
            raise AuthorityIntegrityError(f"{self.path}: the authority log disagrees with its anchor at record {k}: rewritten")

    # ── verification ───────────────────────────────────────────────────────────────────────────────────────
    def verify(self) -> dict:
        """Verify the whole chain, the anchor and every state row against the log. Raises AuthorityIntegrityError; else a summary."""
        rec = self._anchor_rec()
        c = self._conn()
        try:
            c.execute("BEGIN")
            rows = c.execute("SELECT seq, kind, key, body, prev, hash FROM authority_log ORDER BY seq").fetchall()
            head, nxt = self._walk(rows, GENESIS, 1)
            n = nxt - 1
            self._check_anchor(c, n, rec)
            puts: dict = {}
            revoked: set = set()
            epochs: dict = {}
            for seq, kind, key, body, _p, _h in rows:
                d = json.loads(body)
                if kind == "put":
                    puts[(key, int(d["version"]))] = (d["digest"], d["body_sha256"])
                elif kind == "revoke":
                    revoked.add(key)
                elif kind == "revoke_all":
                    epochs[key] = epochs.get(key, 0) + 1
            have = {}
            for mid, ver, digest, body in c.execute("SELECT mission_id, version, digest, body FROM envelopes").fetchall():
                have[(mid, ver)] = (digest, body)
            if set(have) != set(puts):
                raise AuthorityIntegrityError(f"{self.path}: the envelope versions do not match the authority log "
                                              f"(extra: {sorted(set(have) - set(puts))[:3]}, missing: {sorted(set(puts) - set(have))[:3]})")
            for k, (digest, body) in have.items():
                try:
                    self._check_envelope(k[0], k[1], digest, body, puts[k])
                except SelectorError:
                    pass          # bytes match the log (checked above): one unreadable envelope is denied when used; it does not stop the store opening
            if {r[0] for r in c.execute("SELECT mission_id FROM revoked")} != revoked:
                raise AuthorityIntegrityError(f"{self.path}: the revocations do not match the authority log (a revocation was added or deleted)")
            if {t: e for t, e in c.execute("SELECT tenant, epoch FROM epochs") if e} != epochs:
                raise AuthorityIntegrityError(f"{self.path}: a tenant revocation epoch does not match the authority log")
        finally:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            c.close()
        with self._tlock:
            self._seen = (n, head) if n else (0, GENESIS)
        return {"status": "ok", "records": n, "head": head}

    def _check_envelope(self, mid: str, ver: int, digest: str, body: str, logged) -> None:
        want_digest, want_body = logged
        try:
            real = AuthorityEnvelope.from_dict(json.loads(body)).digest()
        except SelectorError:
            # A selector outside the grammar (stored by a release that did not validate): the stored bytes must still be exactly what the log
            # recorded, so this is not tampering; the envelope is unreadable, and the caller turns that into an `authority_malformed` denial.
            if digest != want_digest or sha256(body.encode()).hexdigest() != want_body:
                raise AuthorityIntegrityError(f"{self.path}: envelope {mid} v{ver} does not match the authority log (edited)") from None
            raise
        except Exception:  # noqa: BLE001
            real = ""
        if digest != want_digest or sha256(body.encode()).hexdigest() != want_body or real != want_digest:
            raise AuthorityIntegrityError(f"{self.path}: envelope {mid} v{ver} does not match the authority log (edited)")

    def _guard(self, c, rec: "dict | None") -> None:
        """Incremental check inside the caller's read transaction: the verified tail is unchanged, new records chain, the anchor is
        not ahead. Raises AuthorityIntegrityError."""
        with self._tlock:
            seen = self._seen
        if seen is None:
            raise AuthorityIntegrityError(f"{self.path}: the store was not verified")
        if seen[0]:
            row = c.execute("SELECT hash FROM authority_log WHERE seq=?", (seen[0],)).fetchone()
            if row is None or row[0] != seen[1]:
                raise AuthorityIntegrityError(f"{self.path}: a verified authority record was removed or changed (log truncated or rewritten)")
        rows = c.execute("SELECT seq, kind, key, body, prev, hash FROM authority_log WHERE seq>? ORDER BY seq", (seen[0],)).fetchall()
        head, nxt = self._walk(rows, seen[1], seen[0] + 1)
        self._check_anchor(c, nxt - 1, rec)
        if rows:
            with self._tlock:
                if self._seen is not None and self._seen[0] < nxt - 1:
                    self._seen = (nxt - 1, head)

    def _logged(self, c, kind: str, key: str):
        return c.execute("SELECT body FROM authority_log WHERE kind=? AND key=? ORDER BY seq DESC LIMIT 1", (kind, key)).fetchone()

    # ── reads (each checks what it is about to answer against the log) ─────────────────────────────────────
    def get(self, mission_id):
        rec = self._anchor_rec()
        c = self._conn()
        try:
            c.execute("BEGIN")
            self._guard(c, rec)
            logged = self._logged(c, "put", mission_id)
            r = c.execute("SELECT version, digest, body FROM envelopes WHERE mission_id=? ORDER BY version DESC LIMIT 1", (mission_id,)).fetchone()
            if logged is None:
                if r is not None:
                    raise AuthorityIntegrityError(f"{self.path}: envelope {mission_id} is in the store but not in the authority log")
                return None
            d = json.loads(logged[0])
            if r is None or r[0] != d["version"]:
                raise AuthorityIntegrityError(f"{self.path}: envelope {mission_id} does not match the authority log (latest version missing or replaced)")
            self._check_envelope(mission_id, r[0], r[1], r[2], (d["digest"], d["body_sha256"]))
            return AuthorityEnvelope.from_dict(json.loads(r[2]))
        finally:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            c.close()

    def put(self, env):
        body = json.dumps(env.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

        def go(c):
            v = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM envelopes WHERE mission_id=?", (env.mission_id,)).fetchone()[0]
            now = time.time()
            c.execute("INSERT INTO envelopes (mission_id, version, digest, body, created) VALUES (?,?,?,?,?)",
                      (env.mission_id, v, env.digest(), body, now))
            return self._append(c, "put", env.mission_id, {"version": v, "digest": env.digest(), "body_sha256": sha256(body.encode()).hexdigest(), "at": now})
        self._publish(*self._tx(go))

    def versions(self, mission_id: str) -> list[tuple[int, str]]:
        with self._conn() as c:
            return list(c.execute("SELECT version, digest FROM envelopes WHERE mission_id=? ORDER BY version", (mission_id,)))

    def epoch(self, tenant):
        rec = self._anchor_rec()
        c = self._conn()
        try:
            c.execute("BEGIN")
            self._guard(c, rec)
            r = c.execute("SELECT epoch FROM epochs WHERE tenant=?", (tenant,)).fetchone()
            n = c.execute("SELECT COUNT(*) FROM authority_log WHERE kind='revoke_all' AND key=?", (tenant,)).fetchone()[0]
            if (r[0] if r else 0) != n:
                raise AuthorityIntegrityError(f"{self.path}: the revocation epoch of tenant {tenant!r} does not match the authority log")
            return n
        finally:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            c.close()

    def revoke_all(self, tenant):
        def go(c):
            c.execute("INSERT INTO epochs (tenant, epoch) VALUES (?,1) ON CONFLICT(tenant) DO UPDATE SET epoch=epoch+1", (tenant,))
            e = c.execute("SELECT epoch FROM epochs WHERE tenant=?", (tenant,)).fetchone()[0]
            return e, self._append(c, "revoke_all", tenant, {"epoch": e, "at": time.time()})
        e, rec = self._tx(go)
        self._publish(*rec)
        return e

    def revoke(self, mission_id):
        def go(c):
            now = time.time()
            cur = c.execute("INSERT OR IGNORE INTO revoked (mission_id, at) VALUES (?,?)", (mission_id, now))
            return self._append(c, "revoke", mission_id, {"at": now}) if cur.rowcount == 1 else None
        rec = self._tx(go)
        if rec is not None:
            self._publish(*rec)

    def is_revoked(self, mission_id):
        rec = self._anchor_rec()
        c = self._conn()
        try:
            c.execute("BEGIN")
            self._guard(c, rec)
            row = c.execute("SELECT 1 FROM revoked WHERE mission_id=?", (mission_id,)).fetchone() is not None
            logged = self._logged(c, "revoke", mission_id) is not None
            if row != logged:
                raise AuthorityIntegrityError(f"{self.path}: the revocation of {mission_id!r} does not match the authority log "
                                              f"({'a revocation was deleted' if logged else 'a revocation was added'})")
            return row
        finally:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            c.close()


def _fsync_on() -> bool:
    return os.environ.get("AGENTIC_OS_EVENT_FSYNC", "1").strip().lower() not in ("0", "false", "no", "off")


# ── evaluation ──────────────────────────────────────────────────────────────────────────────────────
def _matches(value: Any, patterns: tuple[str, ...]) -> bool:
    vals = value if isinstance(value, (list, tuple)) else [value]
    return bool(vals) and all(isinstance(v, str) and any(_safe_value(v, p) and fnmatchcase(v, p) for p in patterns) for v in vals)


def _amount(grant: Grant, args: Mapping[str, Any]) -> float | None:
    """The amount an action asks for, or None when it names none. A value that is not a finite, non-negative number comes back as NaN
    (invalid): a negative amount must never lower a running total, so it is not an amount at all."""
    raw = args.get(grant.amount_param)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return float("nan")
    try:
        v = float(raw)
    except (TypeError, ValueError, OverflowError):
        return float("nan")
    return v if math.isfinite(v) and v >= 0 else float("nan")


def _valid_amount(v: float | None) -> bool:
    return v is None or (math.isfinite(v) and v >= 0)


def _clean_actual(actual: "Mapping[str, float] | None") -> "dict[str, float] | None":
    """Settlement figures can only ever consume: a negative or non-finite figure is dropped, so the reserved amount stays charged."""
    if actual is None:
        return None
    out = {}
    for k, v in actual.items():
        try:
            f = float(v)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(f) and f >= 0 and not isinstance(v, bool):
            out[k] = f
    return out


def _bad_delta(deltas: "Mapping[str, float]") -> "str | None":
    """The name of the first counter delta that is negative or not finite, else None. A reservation only ever ADDS consumption, so a
    ledger refuses such a delta instead of letting it lower a running total."""
    for name, d in deltas.items():
        try:
            f = float(d)
        except (TypeError, ValueError, OverflowError):
            return name
        if isinstance(d, bool) or not math.isfinite(f) or f < 0:
            return name
    return None


def evaluate(env: AuthorityEnvelope, action: Action, *, now: float | None = None, current_epoch: int = 0) -> Decision:
    """Pure scope check (no counters). Order: revoked/expired, forbidden, scope, resources, tier, amount."""
    now = time.time() if now is None else now
    dg = env.digest()

    def d(v, rule, reason, amend=None, gi=-1):
        return Decision(v, rule, reason, amend, dg, gi)

    if env.revocation_epoch < current_epoch:
        return d(Verdict.DENY, "revoked", "this authority was revoked")
    if not (now < env.expires_at):
        return d(Verdict.DENY, "expired", "this authority has expired",
                 {"extend_expiry": True})
    for pat in env.forbidden:
        if fnmatchcase(action.capability, pat):
            return d(Verdict.DENY, "forbidden", f"{action.capability} is forbidden for this mission")
    cands = [(i, g) for i, g in enumerate(env.grants) if fnmatchcase(action.capability, g.capability)]
    if not cands:
        return d(Verdict.DENY, "out_of_scope", f"{action.capability} is not granted",
                 {"add_grant": {"capability": action.capability}})
    scope_fail = ""
    best: tuple[Grant, Decision] | None = None
    for gi, g in cands:
        ok = True
        for key, patterns in g.resources:
            if key not in action.arguments or not _matches(action.arguments[key], patterns):
                ok = False
                scope_fail = key
                break
        if not ok:
            continue
        amt = _amount(g, action.arguments)
        limited = g.max_amount is not None or g.max_total_amount is not None
        if limited and not _valid_amount(amt):
            dec = d(Verdict.DENY, "amount_invalid", f"{g.amount_param} is not a finite, non-negative number")
        elif g.max_amount is not None and amt is not None and not (amt <= g.max_amount):
            dec = d(Verdict.ESCALATE, "amount_over_limit",
                    f"{g.amount_param} {amt} exceeds the per-action limit {g.max_amount}",
                    {"raise_limit": {"capability": g.capability, "max_amount": amt}})
        elif g.max_amount is not None and g.amount_param not in action.arguments and action.side_effecting:
            dec = d(Verdict.DENY, "amount_missing", f"{g.amount_param} is required to apply the limit")
        elif action.risk_tier > g.max_tier:
            dec = d(Verdict.ESCALATE, "tier_over_grant",
                    f"risk tier {action.risk_tier} exceeds the tier {g.max_tier} this grant runs unattended",
                    {"raise_tier": {"capability": g.capability, "max_tier": action.risk_tier}})
        else:
            return d(Verdict.ALLOW, "granted", f"covered by grant {g.capability}", gi=gi)
        best = best or (g, dec)
    if best:
        return best[1]
    return d(Verdict.DENY, "resource_out_of_scope", f"argument {scope_fail!r} is outside the granted resources",
             {"add_resource": {"capability": action.capability, "argument": scope_fail}})


@dataclass(frozen=True)
class Standing:
    """What the approval gate that a standing grant is asked to satisfy looked like."""
    risk_fired: bool = False       # a score-based dynamic-risk rule was among the reasons
    risk_waivable: bool = True     # false when the risk is regulatory or the actor is not permissioned: never waived


class AuthorityGuard:
    """Enforcement: scope check, then atomic counter reservation. `envelopes` is trusted state; the
    caller supplies only a mission id, never an envelope."""

    def __init__(self, envelopes: EnvelopeStore, ledger: UsageLedger | None = None, *, clock=time.time) -> None:
        self.envelopes = envelopes
        self.ledger = ledger or MemoryLedger()
        self.clock = clock

    @property
    def durable(self) -> bool:
        return bool(getattr(self.envelopes, "durable", False) and getattr(self.ledger, "durable", False))

    def chain_denial(self, env: "AuthorityEnvelope", now: float) -> "Decision | None":
        """For an attenuated child: every ancestor must still exist, belong to the same tenant, be unrevoked, unexpired and current in
        its revocation epoch, and still COVER what the child holds. A child's authority is therefore always the intersection down its
        chain, checked at every decision: revoking, expiring or narrowing a parent takes effect on all its descendants at once, with
        no need to enumerate them. Anything unreadable fails closed."""
        cur, seen = env, {env.mission_id}
        while cur.parent_mission_id:
            pid = cur.parent_mission_id
            parent = self.envelopes.get(pid)
            if pid in seen or len(seen) > 16:
                return Decision(Verdict.DENY, "bad_chain", "the delegation chain is malformed", None, env.digest())
            seen.add(pid)
            if parent is None or parent.tenant != env.tenant:
                return Decision(Verdict.DENY, "orphaned", "the authority this was delegated from is gone", None, env.digest())
            if self.envelopes.is_revoked(pid) or parent.revocation_epoch < self.envelopes.epoch(parent.tenant):
                return Decision(Verdict.DENY, "ancestor_revoked", "an authority above this one was revoked", None, env.digest())
            if not (now < parent.expires_at):
                return Decision(Verdict.DENY, "ancestor_expired", "an authority above this one has expired", None, env.digest())
            if any(not any(pg.covers(g) for pg in parent.grants) for g in cur.grants):
                return Decision(Verdict.DENY, "exceeds_parent", "the parent's authority no longer covers what was delegated",
                                None, env.digest())
            cur = parent
        return None

    def authorize_delegation(self, mission_id: str, tenant: str) -> tuple[Decision, "AuthorityEnvelope | None"]:
        """May this authority start a child mission? Reserves one unit of the child quota when it may."""
        return _delegation(self, mission_id, tenant)

    def _scope(self, mission_id: str, tenant: str, action: Action, grant_prefix: str = "") -> "tuple[AuthorityEnvelope | None, float, Decision]":
        """The scope half of a decision, with no counters touched: the envelope exists in this tenant, is not revoked, its chain is intact,
        and it covers the action. Returns (envelope, now, decision); the decision is not ALLOW when anything fails."""
        try:
            return self._scope_checked(mission_id, tenant, action, grant_prefix)
        except IntegrityError as e:                  # a tampered store is a refusal that nothing downstream may override, never "no envelope"
            return None, 0.0, Decision(Verdict.DENY, "authority_integrity", f"the authority store failed its integrity check: {e}")
        except SelectorError as e:                   # a stored envelope whose selectors are outside the grammar is refused, never half-read
            return None, 0.0, Decision(Verdict.DENY, "authority_malformed", f"the stored authority has a malformed selector: {e}")

    def _scope_checked(self, mission_id: str, tenant: str, action: Action, grant_prefix: str = "") -> "tuple[AuthorityEnvelope | None, float, Decision]":
        env = self.envelopes.get(mission_id)
        if env is None or env.tenant != tenant:      # unknown, or another tenant's: indistinguishable
            return None, 0.0, Decision(Verdict.DENY, "no_authority", "no authority is recorded for this mission")
        now = self.clock()
        if not grant_prefix and action.capability.startswith(COMPENSATE_PREFIX):
            # the prefix belongs to undo grants: a forward capability may not borrow it (nor be covered by a compensate: grant)
            return env, now, Decision(Verdict.DENY, "reserved_prefix", f"{COMPENSATE_PREFIX!r} is reserved for compensation grants",
                                      None, env.digest())
        if self.envelopes.is_revoked(env.mission_id):
            return env, now, Decision(Verdict.DENY, "revoked", "this authority was revoked", None, env.digest())
        bad = self.chain_denial(env, now)
        if bad is not None:
            return env, now, bad
        if grant_prefix:
            # only grants that name this kind of action count: a wildcard forward grant must not stand in for it
            view = replace(env, grants=tuple(g for g in env.grants if g.capability.startswith(grant_prefix)))
            return env, now, replace(evaluate(view, action, now=now, current_epoch=self.envelopes.epoch(env.tenant)),
                                     envelope_digest=env.digest())
        return env, now, evaluate(env, action, now=now, current_epoch=self.envelopes.epoch(env.tenant))

    def check(self, mission_id: str, tenant: str, action: Action) -> Decision:
        """Would this action be inside the authority RIGHT NOW? Scope, revocation (including every ancestor of a child), expiry and the
        mission's time allowance are checked; nothing is reserved and no counter moves. The gateway uses it to revalidate a caller before
        serving a stored result of an earlier call, which executes nothing and so must not consume the call allowance."""
        env, now, dec = self._scope(mission_id, tenant, action)
        if dec.verdict is not Verdict.ALLOW:
            return dec
        if now - env.issued_at > env.budget.wall_clock_s > 0:
            return Decision(Verdict.DENY, "wall_clock_exhausted", "the mission's time allowance is used up",
                            {"extend_budget": {"wall_clock_s": True}}, dec.envelope_digest)
        return dec

    def check_compensation(self, mission_id: str, tenant: str, capability: str, arguments: "Mapping[str, Any] | None" = None) -> Decision:
        """May the undo of ``capability`` run RIGHT NOW? Only grants whose capability starts with ``compensate:`` are considered (so a
        grant on the forward action, wildcard or not, never implies its undo); revocation, expiry, the delegation chain and the
        forbidden list apply as for any action. Nothing is reserved and no counter moves."""
        action = Action(capability=COMPENSATE_PREFIX + capability, arguments=dict(arguments or {}), risk_tier=1, side_effecting=True)
        env, now, dec = self._scope(mission_id, tenant, action, COMPENSATE_PREFIX)
        if dec.verdict is not Verdict.ALLOW:
            return dec
        if now - env.issued_at > env.budget.wall_clock_s > 0:
            return Decision(Verdict.DENY, "wall_clock_exhausted", "the mission's time allowance is used up",
                            {"extend_budget": {"wall_clock_s": True}}, dec.envelope_digest)
        return dec

    def authorize(self, mission_id: str, tenant: str, action: Action, standing: "Standing | None" = None,
                  lease_id: str = "", lease_meta: "Mapping[str, Any] | None" = None) -> Decision:
        """`standing` is set when the owner's grant is being asked to stand in for an ordinary approval gate:
        the grant must then be explicit (no bare wildcard) and, if a score-based risk gate also fired, must
        opt in to covering it. Without it this is a plain scope + limits check."""
        env, now, dec = self._scope(mission_id, tenant, action)
        if dec.verdict is not Verdict.ALLOW:
            return dec
        if standing is not None:
            g0 = env.grants[dec.grant_index]
            if g0.capability.strip("*") == "":
                return Decision(Verdict.ESCALATE, "grant_not_explicit",
                                "a bare wildcard grant cannot stand in for an approval",
                                {"narrow_grant": {"capability": action.capability}}, dec.envelope_digest)
            if standing.risk_fired and not (standing.risk_waivable and g0.covers_dynamic_risk):
                return Decision(Verdict.ESCALATE, "risk_not_waived",
                                "a score-based risk gate fired and this grant does not cover it",
                                {"raise_grant": {"capability": g0.capability, "covers_dynamic_risk": True}}
                                if standing.risk_waivable else None, dec.envelope_digest)
        gi = dec.grant_index
        g = env.grants[gi]
        amt = _amount(g, action.arguments)
        if not _valid_amount(amt):         # evaluate() refuses this for a limited grant; an unlimited one simply books nothing
            amt = 0.0
        amt = 0.0 if amt is None else amt
        if _bad_delta({"usd": action.cost_usd, "tokens": action.tokens}) is not None:
            return Decision(Verdict.DENY, "amount_invalid", "the action's cost or token estimate is not a finite, non-negative number",
                            None, dec.envelope_digest)
        deltas = {"actions": 1.0, "usd": float(action.cost_usd), "tokens": float(action.tokens),
                  f"g{gi}:calls": 1.0, f"g{gi}:amount": amt}
        limits = {"actions": float(env.budget.max_actions), "usd": float(env.budget.usd),
                  "tokens": float(env.budget.tokens) if env.budget.tokens else None,
                  f"g{gi}:calls": float(g.max_calls) if g.max_calls is not None else None,
                  f"g{gi}:amount": g.max_total_amount}
        if now - env.issued_at > env.budget.wall_clock_s > 0:
            return Decision(Verdict.DENY, "wall_clock_exhausted", "the mission's time allowance is used up",
                            {"extend_budget": {"wall_clock_s": True}}, dec.envelope_digest)
        rate = (f"g{gi}:rate", int(g.rate_calls), g.rate_window_s) if g.rate_calls is not None else None
        if lease_id:                     # idempotent per lease: a retried gate never counts twice
            meta = {"capability": action.capability, "tenant": tenant, "side_effecting": action.side_effecting,
                    "tier": action.risk_tier, "envelope_digest": dec.envelope_digest, **dict(lease_meta or {})}
            refused = self.ledger.reserve_lease(lease_id, env.mission_id, deltas, limits, rate, now, meta)
        else:
            refused = self.ledger.reserve(env.mission_id, deltas, limits, rate, now)
        if refused:
            rule = {"actions": "action_budget", "usd": "usd_budget", "tokens": "token_budget"}.get(
                refused, "rate_limit" if refused.endswith(":rate") else "grant_limit")
            return Decision(Verdict.ESCALATE, rule, f"{rule.replace('_', ' ')} reached ({refused})",
                            {"extend_budget": {refused: True}}, dec.envelope_digest)
        return replace(dec, lease_id=lease_id)

    # ── reservation lifecycle (delegates to the ledger; all transitions are atomic and idempotent) ──
    def dispatched(self, lease_id: str) -> bool:
        """The call is about to leave: from now on the reservation is only released by reconciliation, never by time."""
        return bool(lease_id) and self.ledger.lease_dispatched(lease_id)

    def dispatch_once(self, lease_id: str) -> bool:
        """Atomic: True for exactly one caller, only while the reservation is still RESERVED. This is the single
        door to an execution authorization, so a retry after a timeout or restart can never obtain a second one."""
        return bool(lease_id) and self.ledger.lease_dispatch_once(lease_id)

    def settle(self, lease_id: str, actual: Mapping[str, float] | None = None) -> bool:
        return bool(lease_id) and self.ledger.lease_settle(lease_id, _clean_actual(actual))

    def release(self, lease_id: str) -> bool:
        """Only valid before dispatch (the action provably did not leave)."""
        return bool(lease_id) and self.ledger.lease_release(lease_id)

    def unknown(self, lease_id: str) -> bool:
        return bool(lease_id) and self.ledger.lease_unknown(lease_id)

    def reconcile(self, lease_id: str, executed: bool, actual: Mapping[str, float] | None = None) -> bool:
        """An independent check says the effect did (or did not) happen: settle actual consumption or return it."""
        return bool(lease_id) and self.ledger.lease_reconcile(lease_id, executed, _clean_actual(actual))

    def outstanding(self, key: str | None = None, detail: bool = False) -> list:
        return self.ledger.leases_outstanding(key, detail)

    def annotate(self, lease_id: str, **fields: Any) -> bool:
        """Add evidence to a reservation's durable record (execution id, provider reference, reconciliation status)."""
        return bool(lease_id) and self.ledger.lease_annotate(lease_id, **fields)

    def lease(self, lease_id: str) -> "dict | None":
        return self.ledger.lease_get(lease_id)

    def sweep_undispatched(self, older_than_s: float) -> int:
        """Operator action: release RESERVED leases older than the bound. Never touches DISPATCHED or UNKNOWN."""
        return self.ledger.lease_sweep(older_than_s, self.clock())


def provider_ref_of(result: Any) -> str:
    """The provider's own reference for an effect (invoice id, message id, ...) if the result carries one under a
    conventional key; '' otherwise. Used as reconciliation evidence, never as authority."""
    if isinstance(result, dict):
        for k in ("provider_ref", "provider_object_id", "external_id", "remote_id"):
            v = result.get(k)
            if isinstance(v, (str, int)) and str(v):
                return str(v)[:200]
    return ""


ENV_REQUIRE = "AGENTIC_OS_REQUIRE_AUTHORITY"
ENV_DB = "AGENTIC_OS_AUTHORITY_DB"


def authority_required() -> bool:
    from .profile import is_production
    return is_production()


def authority_from_env() -> "AuthorityGuard | None":
    """Production wiring. With `AGENTIC_OS_AUTHORITY_DB` set, a durable store and ledger back the guard.
    With `AGENTIC_OS_REQUIRE_AUTHORITY=1` and no database, refuse to start rather than run without authority."""
    import os
    path = os.environ.get(ENV_DB, "").strip()
    if not path:
        if authority_required():
            raise RuntimeError(f"production profile but {ENV_DB} is not set: refusing to run without durable authority")
        return None
    return AuthorityGuard(SqliteEnvelopeStore(path), SqliteLedger(path + ".usage"))


def _delegation(guard: "AuthorityGuard", mission_id: str, tenant: str) -> tuple[Decision, "AuthorityEnvelope | None"]:
    try:
        return _delegation_checked(guard, mission_id, tenant)
    except IntegrityError as e:
        return Decision(Verdict.DENY, "authority_integrity", f"the authority store failed its integrity check: {e}"), None


def _delegation_checked(guard: "AuthorityGuard", mission_id: str, tenant: str) -> tuple[Decision, "AuthorityEnvelope | None"]:
    env = guard.envelopes.get(mission_id)
    if env is None or env.tenant != tenant:
        return Decision(Verdict.DENY, "no_authority", "no authority is recorded for this mission"), None
    now = guard.clock()
    if guard.envelopes.is_revoked(env.mission_id) or env.revocation_epoch < guard.envelopes.epoch(env.tenant):
        return Decision(Verdict.DENY, "revoked", "this authority was revoked", None, env.digest()), env
    if not (now < env.expires_at):
        return Decision(Verdict.DENY, "expired", "this authority has expired", None, env.digest()), env
    bad = guard.chain_denial(env, now)
    if bad is not None:
        return bad, env
    if not env.may_delegate:
        return Decision(Verdict.DENY, "delegation_not_allowed", "this authority does not allow delegation", None, env.digest()), env
    refused = guard.ledger.reserve(env.mission_id, {"children": 1.0}, {"children": float(env.budget.max_children)}, None, now)
    if refused:
        return Decision(Verdict.ESCALATE, "child_quota", "the delegation quota is used up",
                        {"extend_budget": {"max_children": True}}, env.digest()), env
    return Decision(Verdict.ALLOW, "delegation_granted", "delegation allowed", None, env.digest()), env


def _jsonable(obj):
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict
        return _jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj
