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
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from enum import Enum
from fnmatch import fnmatchcase
from hashlib import sha256
from typing import Any, Mapping, Protocol

CONTRACT_VERSION = "mission-authority/v1"


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

    def __post_init__(self) -> None:
        for f in ("issued_at", "expires_at"):
            object.__setattr__(self, f, float(getattr(self, f)))
        object.__setattr__(self, "revocation_epoch", int(self.revocation_epoch))
        object.__setattr__(self, "depth", int(self.depth))

    # ── identity ────────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return _jsonable(self)

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
            parent_digest=d.get("parent_digest", ""), depth=int(d.get("depth", 0)))

    def digest(self) -> str:
        canonical = json.dumps(_jsonable(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
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
                       parent_digest=self.digest(), depth=self.depth + 1)


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
                      rate: tuple[str, int, float] | None = None, now: float | None = None) -> str | None: ...
    def lease_state(self, lease_id: str) -> str | None: ...
    def lease_dispatched(self, lease_id: str) -> bool: ...
    def lease_dispatch_once(self, lease_id: str) -> bool: ...     # True only on the RESERVED -> DISPATCHED transition
    def lease_settle(self, lease_id: str, actual: Mapping[str, float] | None = None) -> bool: ...
    def lease_release(self, lease_id: str) -> bool: ...
    def lease_unknown(self, lease_id: str) -> bool: ...
    def lease_reconcile(self, lease_id: str, executed: bool, actual: Mapping[str, float] | None = None) -> bool: ...
    def lease_sweep(self, older_than_s: float, now: float | None = None) -> int: ...
    def leases_outstanding(self, key: str | None = None) -> list[tuple[str, str]]: ...


class MemoryLedger:
    durable = False

    def __init__(self) -> None:
        self._c: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._w: dict[tuple[str, str], deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def reserve(self, key, deltas, limits, rate=None, now=None):
        now = time.time() if now is None else now
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

    def reserve_lease(self, lease_id, key, deltas, limits, rate=None, now=None):
        now = time.time() if now is None else now
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
                                        "rate": (rate[0], now) if rate else None}
            return None

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

    def leases_outstanding(self, key=None):
        with self._lock:
            return sorted((i, r["state"]) for i, r in self._leases().items()
                          if r["state"] in _OPEN and (key is None or r["key"] == key))


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
                      "state TEXT NOT NULL, rate_name TEXT, rate_ts REAL, updated REAL NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def reserve(self, key, deltas, limits, rate=None, now=None):
        now = time.time() if now is None else now
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

    def reserve_lease(self, lease_id, key, deltas, limits, rate=None, now=None):
        now = time.time() if now is None else now

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
            c.execute("INSERT OR REPLACE INTO leases (lease_id,k,deltas,state,rate_name,rate_ts,updated) VALUES (?,?,?,?,?,?,?)",
                      (lease_id, key, json.dumps(dict(deltas), sort_keys=True), Lease.RESERVED.value,
                       rate[0] if rate else None, now if rate else None, now))
            return None
        return self._tx(go)

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

    def leases_outstanding(self, key=None):
        q, a = "SELECT lease_id, state FROM leases WHERE state IN (?,?,?)", list(_OPEN)
        if key is not None:
            q, a = q + " AND k=?", a + [key]
        with self._conn() as c:
            return sorted((r[0], r[1]) for r in c.execute(q, a))


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


class SqliteEnvelopeStore:
    """Durable authority: survives restarts and is shared by replicas on the same database file. Every `put`
    is a new immutable version (history is kept for audit); `get` returns the latest. Revocation is durable
    and takes effect on the next decision of any process."""
    durable = True

    def __init__(self, path: str) -> None:
        self.path = path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS envelopes (mission_id TEXT, version INTEGER, digest TEXT NOT NULL, "
                      "body TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (mission_id, version))")
            c.execute("CREATE TABLE IF NOT EXISTS epochs (tenant TEXT PRIMARY KEY, epoch INTEGER NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS revoked (mission_id TEXT PRIMARY KEY, at REAL NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def get(self, mission_id):
        c = self._conn()
        try:
            r = c.execute("SELECT body FROM envelopes WHERE mission_id=? ORDER BY version DESC LIMIT 1", (mission_id,)).fetchone()
        finally:
            c.close()
        return AuthorityEnvelope.from_dict(json.loads(r[0])) if r else None

    def put(self, env):
        body = json.dumps(env.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            v = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM envelopes WHERE mission_id=?", (env.mission_id,)).fetchone()[0]
            c.execute("INSERT INTO envelopes (mission_id, version, digest, body, created) VALUES (?,?,?,?,?)",
                      (env.mission_id, v, env.digest(), body, time.time()))
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    def versions(self, mission_id: str) -> list[tuple[int, str]]:
        with self._conn() as c:
            return list(c.execute("SELECT version, digest FROM envelopes WHERE mission_id=? ORDER BY version", (mission_id,)))

    def epoch(self, tenant):
        with self._conn() as c:
            r = c.execute("SELECT epoch FROM epochs WHERE tenant=?", (tenant,)).fetchone()
        return r[0] if r else 0

    def revoke_all(self, tenant):
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO epochs (tenant, epoch) VALUES (?,1) ON CONFLICT(tenant) DO UPDATE SET epoch=epoch+1", (tenant,))
            e = c.execute("SELECT epoch FROM epochs WHERE tenant=?", (tenant,)).fetchone()[0]
            c.execute("COMMIT")
            return e
        finally:
            c.close()

    def revoke(self, mission_id):
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO revoked (mission_id, at) VALUES (?,?)", (mission_id, time.time()))

    def is_revoked(self, mission_id):
        with self._conn() as c:
            return c.execute("SELECT 1 FROM revoked WHERE mission_id=?", (mission_id,)).fetchone() is not None


# ── evaluation ──────────────────────────────────────────────────────────────────────────────────────
def _matches(value: Any, patterns: tuple[str, ...]) -> bool:
    vals = value if isinstance(value, (list, tuple)) else [value]
    return bool(vals) and all(isinstance(v, str) and any(fnmatchcase(v, p) for p in patterns) for v in vals)


def _amount(grant: Grant, args: Mapping[str, Any]) -> float | None:
    raw = args.get(grant.amount_param)
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return float("nan")
    return v


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
        if g.max_amount is not None and amt is not None and not (amt <= g.max_amount):
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

    def authorize_delegation(self, mission_id: str, tenant: str) -> tuple[Decision, "AuthorityEnvelope | None"]:
        """May this authority start a child mission? Reserves one unit of the child quota when it may."""
        return _delegation(self, mission_id, tenant)

    def authorize(self, mission_id: str, tenant: str, action: Action, standing: "Standing | None" = None,
                  lease_id: str = "") -> Decision:
        """`standing` is set when the owner's grant is being asked to stand in for an ordinary approval gate:
        the grant must then be explicit (no bare wildcard) and, if a score-based risk gate also fired, must
        opt in to covering it. Without it this is a plain scope + limits check."""
        env = self.envelopes.get(mission_id)
        if env is None or env.tenant != tenant:      # unknown, or another tenant's: indistinguishable
            return Decision(Verdict.DENY, "no_authority", "no authority is recorded for this mission")
        now = self.clock()
        if self.envelopes.is_revoked(env.mission_id):
            return Decision(Verdict.DENY, "revoked", "this authority was revoked", None, env.digest())
        dec = evaluate(env, action, now=now, current_epoch=self.envelopes.epoch(env.tenant))
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
        amt = 0.0 if amt is None else amt
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
            refused = self.ledger.reserve_lease(lease_id, env.mission_id, deltas, limits, rate, now)
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
        return bool(lease_id) and self.ledger.lease_settle(lease_id, actual)

    def release(self, lease_id: str) -> bool:
        """Only valid before dispatch (the action provably did not leave)."""
        return bool(lease_id) and self.ledger.lease_release(lease_id)

    def unknown(self, lease_id: str) -> bool:
        return bool(lease_id) and self.ledger.lease_unknown(lease_id)

    def reconcile(self, lease_id: str, executed: bool, actual: Mapping[str, float] | None = None) -> bool:
        """An independent check says the effect did (or did not) happen: settle actual consumption or return it."""
        return bool(lease_id) and self.ledger.lease_reconcile(lease_id, executed, actual)

    def outstanding(self, key: str | None = None) -> list[tuple[str, str]]:
        return self.ledger.leases_outstanding(key)

    def sweep_undispatched(self, older_than_s: float) -> int:
        """Operator action: release RESERVED leases older than the bound. Never touches DISPATCHED or UNKNOWN."""
        return self.ledger.lease_sweep(older_than_s, self.clock())


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
    env = guard.envelopes.get(mission_id)
    if env is None or env.tenant != tenant:
        return Decision(Verdict.DENY, "no_authority", "no authority is recorded for this mission"), None
    now = guard.clock()
    if guard.envelopes.is_revoked(env.mission_id) or env.revocation_epoch < guard.envelopes.epoch(env.tenant):
        return Decision(Verdict.DENY, "revoked", "this authority was revoked", None, env.digest()), env
    if not (now < env.expires_at):
        return Decision(Verdict.DENY, "expired", "this authority has expired", None, env.digest()), env
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
