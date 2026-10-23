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

    def __post_init__(self) -> None:
        # one canonical number type per field, so a digest is the same before and after a JSON round-trip
        for f, t in (("max_amount", float), ("max_total_amount", float), ("max_calls", int), ("rate_calls", int)):
            v = getattr(self, f)
            object.__setattr__(self, f, None if v is None else t(v))
        object.__setattr__(self, "max_tier", int(self.max_tier))
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
        if other.max_tier > self.max_tier:
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
            rate_window_s=float(g.get("rate_window_s", 3600.0))) for g in d.get("grants", ()))
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


class MemoryLedger:
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


class SqliteLedger:
    """Durable, multi-process counters (one `BEGIN IMMEDIATE` transaction per reserve)."""
    def __init__(self, path: str) -> None:
        self.path = path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS usage (k TEXT, name TEXT, v REAL NOT NULL, PRIMARY KEY (k, name))")
            c.execute("CREATE TABLE IF NOT EXISTS hits (k TEXT, name TEXT, ts REAL NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS hits_idx ON hits (k, name, ts)")

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


# ── trusted storage ─────────────────────────────────────────────────────────────────────────────────
class EnvelopeStore(Protocol):
    def get(self, mission_id: str) -> AuthorityEnvelope | None: ...
    def put(self, env: AuthorityEnvelope) -> None: ...
    def epoch(self, tenant: str) -> int: ...
    def revoke_all(self, tenant: str) -> int: ...


class MemoryEnvelopeStore:
    """Authoritative store. Only owner/platform code calls `put`/`revoke_all`; the gateway only calls `get`."""
    def __init__(self) -> None:
        self._e: dict[str, AuthorityEnvelope] = {}
        self._epoch: dict[str, int] = defaultdict(int)

    def get(self, mission_id):
        return self._e.get(mission_id)

    def put(self, env):
        self._e[env.mission_id] = env

    def epoch(self, tenant):
        return self._epoch[tenant]

    def revoke_all(self, tenant):
        self._epoch[tenant] += 1
        return self._epoch[tenant]


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


class AuthorityGuard:
    """Enforcement: scope check, then atomic counter reservation. `envelopes` is trusted state; the
    caller supplies only a mission id, never an envelope."""

    def __init__(self, envelopes: EnvelopeStore, ledger: UsageLedger | None = None, *, clock=time.time) -> None:
        self.envelopes = envelopes
        self.ledger = ledger or MemoryLedger()
        self.clock = clock

    def authorize_delegation(self, mission_id: str, tenant: str) -> tuple[Decision, "AuthorityEnvelope | None"]:
        """May this authority start a child mission? Reserves one unit of the child quota when it may."""
        return _delegation(self, mission_id, tenant)

    def authorize(self, mission_id: str, tenant: str, action: Action) -> Decision:
        env = self.envelopes.get(mission_id)
        if env is None or env.tenant != tenant:      # unknown, or another tenant's: indistinguishable
            return Decision(Verdict.DENY, "no_authority", "no authority is recorded for this mission")
        now = self.clock()
        dec = evaluate(env, action, now=now, current_epoch=self.envelopes.epoch(env.tenant))
        if dec.verdict is not Verdict.ALLOW:
            return dec
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
        refused = self.ledger.reserve(env.mission_id, deltas, limits, rate, now)
        if refused:
            rule = {"actions": "action_budget", "usd": "usd_budget", "tokens": "token_budget"}.get(
                refused, "rate_limit" if refused.endswith(":rate") else "grant_limit")
            return Decision(Verdict.ESCALATE, rule, f"{rule.replace('_', ' ')} reached ({refused})",
                            {"extend_budget": {refused: True}}, dec.envelope_digest)
        return dec


def _delegation(guard: "AuthorityGuard", mission_id: str, tenant: str) -> tuple[Decision, "AuthorityEnvelope | None"]:
    env = guard.envelopes.get(mission_id)
    if env is None or env.tenant != tenant:
        return Decision(Verdict.DENY, "no_authority", "no authority is recorded for this mission"), None
    now = guard.clock()
    if env.revocation_epoch < guard.envelopes.epoch(env.tenant):
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
