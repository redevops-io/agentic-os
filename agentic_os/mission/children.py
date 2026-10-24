"""Child agents: a delegated worker is an identity with its own authority, its own credential and a place in a lineage, never a borrowed copy
of its parent's.

Before this module a child was only a mission started under the parent's envelope: it acted as the parent, spent the parent's budget
without a share of its own, could not be told apart in an audit, and could not be switched off without switching off the parent.
`AuthorityEnvelope.attenuate` already knew how to make a provably smaller envelope, but nothing stored one, enforced it or tied it to a
caller. This is that wiring.

What spawning a child does, in order (every step can refuse, and a refusal undoes the earlier steps):

  1. The parent's authority must allow delegation (`authorize_delegation`): not revoked or expired, `may_delegate`, a unit of the child
     quota (`max_children`), and a live chain of its own.
  2. The child envelope is derived with `attenuate`: never a grant, limit, lifetime or depth the parent does not have.
  3. The child's allowance is CARVED OUT of the parent's counters (usd, tokens, actions) as a reservation. The parent cannot spend what it
     handed out, children can never spend more than the parent had left, and the sum over a tree is bounded by its root. When the child
     ends, what it did not use is returned (settle with the actual).
  4. The child is recorded in a durable registry with an atomic cap on how many children of one parent are active at once
     (`max_parallel`) and on how many are active in the whole tree.
  5. The envelope is stored and a signed, short-lived CREDENTIAL is issued, bound to the child's identity, tenant, lineage, envelope digest
     and (optionally) the client that may present it.

Enforcement (not trust): `AuthorityGuard.chain_denial` walks the chain at every decision. A child's authority is the intersection down its
chain: revoke, expire or narrow any ancestor and every descendant is denied at once, with no list to keep in sync. The gateway takes the
envelope key from a VERIFIED credential, never from a name the caller supplies.

The credential lives as long as the mission runs, as ONE token: it is not rotated or re-issued mid-mission. Its "short life" is a LIVENESS
LEASE, not a fixed lifetime. The token itself expires only with the child's envelope (the hard cap). It verifies only while the registry's
lease for the child is current, and the lease is extended by the OWNER's heartbeat (`keepalive` / `heartbeat`), never by the token being
used, so a stolen token cannot keep itself alive. While the mission runs the heartbeat keeps the same token valid for as long as the work
takes; when the mission ends or its driver dies the heartbeat stops and the token dies within one lease period (`lease_ttl_s`). A lapse is
not a revocation: when the driver comes back and beats again the same token works again; close, revoke and envelope expiry are final.

Limits, stated plainly: the credential is a bearer token (reusable while its lease is live), so it is bindable to one client, revocable
instantly (the registry is consulted on every verification) and never wider than the child's envelope; every action
still passes the guard and counts against the child's counters. A crash between steps can leak a unit of quota or a reservation (it is
counted, never lost); it cannot create authority.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as _secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .approval import _b64, _secret_bytes, _unb64, canonical
from .authority import AuthorityEnvelope, AuthorityError, AutonomyBudget, Grant, Verdict

KIND = "child_cred/v1"
ACTIVE, CLOSED, REVOKED = "active", "closed", "revoked"


class SpawnRefused(Exception):
    def __init__(self, code: str, reason: str, amendment: Optional[dict] = None):
        super().__init__(f"{code}: {reason}")
        self.code, self.reason, self.amendment = code, reason, amendment


@dataclass(frozen=True)
class ChildAgent:
    child_id: str
    principal: str                 # the identity every action and audit record carries
    key: str                       # the child's envelope / counter key
    tenant: str
    parent_key: str
    role: str
    depth: int
    credential: str
    expires_at: float
    envelope_digest: str
    allowance: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Verified:
    ok: bool
    claims: Optional[dict] = None
    reason: str = ""               # for the audit log only; callers show the outside world nothing more than "invalid"


# ───────────────────────────── registry ─────────────────────────────
class ChildRegistry:
    """Durable lineage and status, shared by every replica on the same database. The source of truth for 'is this child still active'."""

    def __init__(self, path: str) -> None:
        self.path = path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS children (child_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, parent_key TEXT NOT NULL, "
                      "root_key TEXT NOT NULL, principal TEXT NOT NULL, role TEXT NOT NULL, depth INTEGER NOT NULL, status TEXT NOT NULL, "
                      "spawned_at REAL NOT NULL, expires_at REAL NOT NULL, jti TEXT NOT NULL, carve_lease TEXT NOT NULL, "
                      "bound_client TEXT NOT NULL DEFAULT '', closed_at REAL, reason TEXT NOT NULL DEFAULT '', "
                      "lease_until REAL NOT NULL DEFAULT 0)")
            if "lease_until" not in {r[1] for r in c.execute("PRAGMA table_info(children)")}:      # a registry created before leases
                c.execute("ALTER TABLE children ADD COLUMN lease_until REAL NOT NULL DEFAULT 0")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS children_principal ON children (tenant, principal)")
            c.execute("CREATE INDEX IF NOT EXISTS children_parent ON children (parent_key, status)")
            c.execute("CREATE TABLE IF NOT EXISTS child_audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, child_id TEXT, type TEXT, "
                      "payload TEXT, ts REAL)")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.row_factory = sqlite3.Row
        return c

    def insert(self, row: dict, *, max_active_children: int, max_active_tree: int, now: float) -> Optional[str]:
        """Atomically add a child unless the parent already has `max_active_children` active, or its tree has `max_active_tree`.
        None on success, else the reason."""
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            n_parent = c.execute("SELECT COUNT(*) FROM children WHERE parent_key=? AND status='active' AND expires_at>?",
                                 (row["parent_key"], now)).fetchone()[0]
            if n_parent >= max_active_children:
                c.execute("ROLLBACK")
                return "max_parallel"
            n_tree = c.execute("SELECT COUNT(*) FROM children WHERE root_key=? AND status='active' AND expires_at>?",
                               (row["root_key"], now)).fetchone()[0]
            if n_tree >= max_active_tree:
                c.execute("ROLLBACK")
                return "max_tree"
            c.execute("INSERT INTO children (child_id, tenant, parent_key, root_key, principal, role, depth, status, spawned_at, expires_at, "
                      "jti, carve_lease, bound_client, lease_until) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (row["child_id"], row["tenant"], row["parent_key"], row["root_key"], row["principal"], row["role"], row["depth"],
                       ACTIVE, now, row["expires_at"], row["jti"], row["carve_lease"], row.get("bound_client", ""),
                       float(row["lease_until"])))
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

    def get(self, child_id: str) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM children WHERE child_id=?", (child_id,)).fetchone()
        return dict(r) if r else None

    def by_principal(self, tenant: str, principal: str) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM children WHERE tenant=? AND principal=?", (tenant, principal)).fetchone()
        return dict(r) if r else None

    def children_of(self, parent_key: str) -> list:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM children WHERE parent_key=? ORDER BY spawned_at, child_id", (parent_key,))]

    def descendants(self, key: str) -> list:
        """Every descendant, deepest first (so a cascade closes leaves before their parents)."""
        out, frontier = [], [key]
        while frontier:
            nxt = []
            for k in frontier:
                for r in self.children_of(k):
                    out.append(r)
                    nxt.append(r["child_id"])
            frontier = nxt
        return list(reversed(out))

    def set_status(self, child_id: str, status: str, reason: str, now: float) -> bool:
        """active -> closed | revoked, once. False if it was already finished (the first decision stands)."""
        with self._conn() as c:
            cur = c.execute("UPDATE children SET status=?, closed_at=?, reason=? WHERE child_id=? AND status='active'",
                            (status, now, reason[:300], child_id))
            return cur.rowcount == 1

    def touch(self, child_id: str, until: float) -> bool:
        """Extend the liveness lease of an ACTIVE child (never shortens it). False for a child that is finished or unknown."""
        with self._conn() as c:
            return c.execute("UPDATE children SET lease_until=MAX(lease_until, ?) WHERE child_id=? AND status='active'",
                             (until, child_id)).rowcount == 1

    def lapsed(self, before: float) -> list:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM children WHERE status='active' AND lease_until<?", (before,))]

    def rotate(self, child_id: str, jti: str) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE children SET jti=? WHERE child_id=? AND status='active'", (jti, child_id)).rowcount == 1

    def audit(self, child_id: str, type_: str, payload: dict, now: float) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO child_audit (child_id, type, payload, ts) VALUES (?,?,?,?)",
                      (child_id, type_, json.dumps(payload, default=str, sort_keys=True), now))

    def audit_log(self, child_id: Optional[str] = None) -> list:
        with self._conn() as c:
            q, a = ("SELECT * FROM child_audit WHERE child_id=? ORDER BY seq", (child_id,)) if child_id else \
                   ("SELECT * FROM child_audit ORDER BY seq", ())
            return [{"seq": r["seq"], "child_id": r["child_id"], "type": r["type"], "payload": json.loads(r["payload"]), "ts": r["ts"]}
                    for r in c.execute(q, a)]

    def key_for_principal(self, tenant: str, principal: str) -> Optional[str]:
        """The envelope key a child principal acts under (for a runtime whose `authority_key` must resolve a child's missions)."""
        r = self.by_principal(tenant, principal)
        return r["child_id"] if r and r["status"] == ACTIVE else None


# ───────────────────────────── credentials ─────────────────────────────
def _ckey(secret: "bytes | str | None") -> bytes:
    base = _secret_bytes(secret)
    if not base:
        return b""
    return hmac.new(base, b"redevops/child-agent/v1", hashlib.sha256).digest()      # domain-separated from approvals and exec auth


def mint_child_credential(secret, *, tenant: str, child_id: str, principal: str, parent_key: str, role: str, depth: int,
                          envelope_digest: str, exp: float, jti: str, bound_client: str = "") -> Optional[str]:
    key = _ckey(secret)
    if not key or not (tenant and child_id and principal and jti):
        return None                                   # never mint an unbound or unsigned credential
    payload = canonical({"kind": KIND, "tenant": tenant, "child_id": child_id, "principal": principal, "parent_key": parent_key,
                         "role": role, "depth": int(depth), "envelope_digest": envelope_digest, "exp": float(exp), "jti": jti,
                         "client": bound_client})
    return _b64(payload) + "." + _b64(hmac.new(key, payload, hashlib.sha256).digest())


class ChildCredentialVerifier:
    """`verify(token, tenant=, client=)` never raises and fails closed. A credential is honoured only while ALL of these hold: the
    signature, its expiry, the tenant and client it is bound to, an ACTIVE registry row carrying this very token id (so rotation and
    close kill old tokens at once), the stored envelope still having the digest the credential names, and every ancestor still valid."""

    def __init__(self, guard, registry: ChildRegistry, secret, *, clock: Callable[[], float] = time.time) -> None:
        self.guard, self.registry, self.secret, self.clock = guard, registry, secret, clock

    def verify(self, token: object, *, tenant: Optional[str] = None, client: str = "", now: Optional[float] = None) -> Verified:
        key = _ckey(self.secret)
        if not key or not isinstance(token, str):
            return Verified(False, None, "no credential")
        try:
            p64, s64 = token.split(".")
            payload, sig = _unb64(p64), _unb64(s64)
            if not hmac.compare_digest(hmac.new(key, payload, hashlib.sha256).digest(), sig):
                return Verified(False, None, "bad signature")
            d = json.loads(payload)
            t = self.clock() if now is None else now
            if not (isinstance(d, dict) and d.get("kind") == KIND):
                return Verified(False, None, "wrong kind")
            if not float(d["exp"]) > t:
                return Verified(False, None, "expired")
            if tenant is not None and d["tenant"] != tenant:
                return Verified(False, None, "another tenant's credential")
            if d.get("client") and d["client"] != client:
                return Verified(False, None, "bound to another client")
            row = self.registry.get(d["child_id"])
            if row is None or row["status"] != ACTIVE:
                return Verified(False, None, "the child is not active")
            if row["jti"] != d["jti"] or row["principal"] != d["principal"] or row["tenant"] != d["tenant"]:
                return Verified(False, None, "superseded credential")
            if not t < float(row["expires_at"]):
                return Verified(False, None, "the child has expired")
            if not t < float(row["lease_until"]):
                return Verified(False, None, "the child's driver has stopped renewing its lease")
            env = self.guard.envelopes.get(row["child_id"])
            if env is None or env.digest() != d["envelope_digest"]:
                return Verified(False, None, "the envelope is not the one this credential was issued for")
            if self.guard.envelopes.is_revoked(env.mission_id) or env.revocation_epoch < self.guard.envelopes.epoch(env.tenant):
                return Verified(False, None, "revoked")
            if not t < env.expires_at:
                return Verified(False, None, "the envelope has expired")
            bad = self.guard.chain_denial(env, t)
            if bad is not None:
                return Verified(False, None, f"chain: {bad.rule}")
            return Verified(True, {"child_id": d["child_id"], "key": d["child_id"], "principal": d["principal"], "tenant": d["tenant"],
                                   "parent_key": d["parent_key"], "role": d["role"], "depth": int(d["depth"]),
                                   "envelope_digest": d["envelope_digest"]}, "")
        except Exception as e:  # noqa: BLE001 - a malformed token of any shape fails closed
            return Verified(False, None, f"malformed: {type(e).__name__}")


class _KeepAlive:
    def __init__(self, spawner: "ChildSpawner", child_id: str) -> None:
        self.spawner, self.child_id = spawner, child_id
        self._stop = __import__("threading").Event()
        self._thread = None

    def __enter__(self) -> "_KeepAlive":
        import threading  # noqa: PLC0415
        if not self.spawner.heartbeat(self.child_id):
            raise SpawnRefused("not_active", "this child cannot be kept alive (finished, expired or its authority is gone)")
        self._thread = threading.Thread(target=self._beat, name=f"child-{self.child_id}", daemon=True)
        self._thread.start()
        return self

    def _beat(self) -> None:
        while not self._stop.wait(max(0.01, self.spawner.lease_ttl_s / 3.0)):
            if not self.spawner.heartbeat(self.child_id):
                return                                    # closed, revoked or out of authority: nothing to keep alive

    def __exit__(self, *exc) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        return False


# ───────────────────────────── spawning ─────────────────────────────
class ChildSpawner:
    def __init__(self, guard, registry: ChildRegistry, secret, *, clock: Callable[[], float] = time.time,
                 on_event: Optional[Callable[[dict], None]] = None, lease_ttl_s: float = 60.0, max_active_tree: int = 32) -> None:
        if not getattr(guard, "durable", False):
            from .profile import is_production  # noqa: PLC0415
            if is_production():
                raise SpawnRefused("not_durable", "the production profile requires a durable authority guard to spawn child agents")
        self.guard, self.registry, self.secret, self.clock = guard, registry, secret, clock
        self.on_event, self.lease_ttl_s, self.max_active_tree = on_event or (lambda ev: None), float(lease_ttl_s), int(max_active_tree)
        self._verifier = ChildCredentialVerifier(guard, registry, secret, clock=clock)

    def verifier(self) -> ChildCredentialVerifier:
        return self._verifier

    def _emit(self, ev_type: str, **extra) -> None:
        try:
            self.on_event({"type": ev_type, "ts": self.clock(), **extra})
        except Exception:  # noqa: BLE001 - an observer must never change a decision
            pass

    def _refuse(self, parent_key: str, role: str, code: str, reason: str, amendment: Optional[dict] = None):
        self.registry.audit(parent_key, "refused", {"role": role, "code": code, "reason": reason}, self.clock())
        self._emit("CHILD_REFUSED", parent=parent_key, role=role, code=code)
        raise SpawnRefused(code, reason, amendment)

    def spawn(self, parent_key: str, tenant: str, *, role: str, goal: str, grants: tuple, budget: AutonomyBudget, ttl_s: float = 3600.0,
              may_delegate: bool = False, client: str = "") -> ChildAgent:
        g, now = self.guard, self.clock()
        if not _ckey(self.secret):
            self._refuse(parent_key, role, "no_signing_secret", "no secret is configured to sign child credentials")
        if not str(role).strip() or not grants:
            self._refuse(parent_key, role, "invalid", "a role and at least one grant are required")
        parent = g.envelopes.get(parent_key)
        # 1. may the parent delegate at all (revoked, expired, chain, may_delegate, quota)? Reserves one unit of the child quota.
        dec, _ = g.authorize_delegation(parent_key, tenant)
        if not dec.allowed:
            self._refuse(parent_key, role, dec.rule, dec.reason, dec.amendment)
        released = {"quota": False, "carve": False}

        def undo() -> None:
            if not released["quota"]:
                g.ledger.release(parent_key, {"children": 1.0})
                released["quota"] = True
        child_id = "agent_" + hashlib.sha256(f"{parent_key}|{role}|{uuid.uuid4().hex}".encode()).hexdigest()[:16]
        principal = f"agent:{(parent.principal if parent else parent_key)}/{role}/{child_id[-8:]}"
        try:
            # 2. a provably smaller envelope
            exp = now + float(ttl_s)
            env = parent.attenuate(mission_id=child_id, principal=principal, grants=tuple(grants), budget=budget, goal=goal,
                                   expires_at=exp, may_delegate=may_delegate, now=now)
        except AuthorityError as e:
            undo()
            self._refuse(parent_key, role, e.code, str(e))
        # 3. carve the allowance out of the parent's counters (it cannot spend what it handed out)
        lease = f"spawn:{child_id}"
        deltas = {"usd": float(budget.usd), "tokens": float(budget.tokens), "actions": float(budget.max_actions)}
        limits = {"usd": float(parent.budget.usd), "actions": float(parent.budget.max_actions),
                  "tokens": float(parent.budget.tokens) if parent.budget.tokens else None}
        refused = g.ledger.reserve_lease(lease, parent_key, deltas, limits, None, now,
                                         {"kind": "child_allowance", "child_id": child_id, "role": role, "tenant": tenant})
        if refused:
            undo()
            self._refuse(parent_key, role, "allowance_unavailable", f"the parent has no {refused} left to hand to a child",
                         {"extend_budget": {refused: True}})
        jti = _secrets.token_hex(12)
        root = self._root_of(parent_key)
        try:
            # 4. the registry decides how many may be active at once, atomically
            why = self.registry.insert({"child_id": child_id, "tenant": tenant, "parent_key": parent_key, "root_key": root,
                                        "principal": principal, "role": role, "depth": env.depth, "expires_at": env.expires_at,
                                        "jti": jti, "carve_lease": lease, "bound_client": client, "lease_until": now + self.lease_ttl_s},
                                       max_active_children=max(1, int(parent.budget.max_parallel)),
                                       max_active_tree=self.max_active_tree, now=now)
            if why is not None:
                g.release(lease)
                undo()
                self._refuse(parent_key, role, why, f"too many active children ({why})")
            g.dispatch_once(lease)               # from here the allowance is only returned by settling what the child actually used
            # 5. the stored envelope and the credential
            g.envelopes.put(env)
            tok = mint_child_credential(self.secret, tenant=tenant, child_id=child_id, principal=principal, parent_key=parent_key,
                                        role=role, depth=env.depth, envelope_digest=env.digest(),
                                        exp=env.expires_at, jti=jti, bound_client=client)
        except SpawnRefused:
            raise
        except BaseException:
            self.registry.set_status(child_id, REVOKED, "spawn failed", self.clock())
            g.envelopes.revoke(child_id)
            raise
        agent = ChildAgent(child_id=child_id, principal=principal, key=child_id, tenant=tenant, parent_key=parent_key, role=role,
                           depth=env.depth, credential=tok or "", expires_at=env.expires_at, envelope_digest=env.digest(),
                           allowance={"usd": budget.usd, "tokens": budget.tokens, "actions": budget.max_actions})
        self.registry.audit(child_id, "spawned", {"parent": parent_key, "principal": principal, "role": role, "depth": env.depth,
                                                  "envelope": env.digest(), "allowance": agent.allowance, "client": client}, now)
        self._emit("CHILD_SPAWNED", child_id=child_id, parent=parent_key, principal=principal, role=role, depth=env.depth)
        return agent

    def _root_of(self, key: str) -> str:
        row, cur = self.registry.get(key), key
        while row is not None:
            cur = row["parent_key"]
            row = self.registry.get(cur)
        return cur

    def renew_credential(self, child_id: str, *, client: Optional[str] = None) -> ChildAgent:
        """A DELIBERATE rotation (a suspected leak, a change of bound client): a new token id, every earlier one stops verifying at once.
        Not needed to keep a running mission going: the lease heartbeat does that with the same token."""
        row = self.registry.get(child_id)
        env = self.guard.envelopes.get(child_id)
        now = self.clock()
        if row is None or row["status"] != ACTIVE or env is None or not now < env.expires_at:
            raise SpawnRefused("not_active", "this child is not active")
        jti = _secrets.token_hex(12)
        bound = row["bound_client"] if client is None else client
        tok = mint_child_credential(self.secret, tenant=row["tenant"], child_id=child_id, principal=row["principal"], parent_key=row["parent_key"],
                                    role=row["role"], depth=row["depth"], envelope_digest=env.digest(),
                                    exp=env.expires_at, jti=jti, bound_client=bound)
        self.registry.rotate(child_id, jti)
        self.registry.audit(child_id, "credential_renewed", {}, now)
        return ChildAgent(child_id=child_id, principal=row["principal"], key=child_id, tenant=row["tenant"], parent_key=row["parent_key"],
                          role=row["role"], depth=row["depth"], credential=tok or "", expires_at=env.expires_at, envelope_digest=env.digest())

    # ── liveness: the credential lives while the mission runs ──
    def heartbeat(self, child_id: str) -> bool:
        """The OWNER's statement that this child's driver is alive: extend its lease by `lease_ttl_s`, same token, nothing rotated. Refused (False)
        for a child that is finished, expired, or whose chain is no longer valid: a heartbeat can revive a lapse, never a revocation."""
        now = self.clock()
        row = self.registry.get(child_id)
        env = self.guard.envelopes.get(child_id)
        if row is None or row["status"] != ACTIVE or env is None or not now < min(env.expires_at, float(row["expires_at"])):
            return False
        if self.guard.envelopes.is_revoked(env.mission_id) or env.revocation_epoch < self.guard.envelopes.epoch(env.tenant):
            return False
        if self.guard.chain_denial(env, now) is not None:
            return False
        return self.registry.touch(child_id, now + self.lease_ttl_s)

    def keepalive(self, child_id: str) -> "_KeepAlive":
        """`with spawner.keepalive(child.child_id): run the child's work` beats every third of the lease period for as long as the work runs,
        and stops when it ends (or the child is closed/revoked), after which the token dies within one lease period."""
        return _KeepAlive(self, child_id)

    def reap_lapsed(self, grace_s: float = 300.0) -> list:
        """A child whose driver has not beaten for `grace_s` beyond its lease is treated as abandoned: closed, its unused allowance returned,
        anything it left unresolved reported. Run on a schedule; a child still being heartbeat is never touched."""
        return [self.close(r["child_id"], f"driver lost: no heartbeat for {int(grace_s)}s beyond its lease") for r in
                self.registry.lapsed(self.clock() - grace_s)]

    # ── ending a child ──
    def close(self, child_id: str, reason: str = "done", *, status: str = CLOSED, actor: str = "") -> dict:
        """End a child (and, first, everything below it): its authority stops now, what it did not use goes back to the parent, and anything
        it left unresolved (a dispatched or unknown reservation) is reported, never dropped."""
        for d in self.registry.descendants(child_id):
            self._end(d["child_id"], f"ancestor {child_id} ended: {reason}", status, actor)
        return self._end(child_id, reason, status, actor)

    def revoke(self, child_id: str, *, actor: str, reason: str) -> dict:
        if not str(actor).strip() or len(str(reason).strip()) < 8:
            raise SpawnRefused("invalid", "the person revoking must be named and give a reason")
        return self.close(child_id, reason, status=REVOKED, actor=actor.strip())

    def close_tree(self, parent_key: str, reason: str = "parent finished") -> list:
        """The parent's work is over: end every child it still has (cancellation propagates down the lineage)."""
        return [self.close(c["child_id"], reason) for c in self.registry.children_of(parent_key) if c["status"] == ACTIVE]

    def _end(self, child_id: str, reason: str, status: str, actor: str) -> dict:
        g, now = self.guard, self.clock()
        row = self.registry.get(child_id)
        if row is None:
            raise SpawnRefused("unknown", "no such child")
        if row["status"] != ACTIVE:
            return {"child_id": child_id, "status": row["status"], "already": True}
        g.envelopes.revoke(child_id)                         # authority stops first: nothing more can be reserved
        used = g.ledger.used(child_id)
        actual = {"usd": float(used.get("usd", 0.0)), "tokens": float(used.get("tokens", 0.0)), "actions": float(used.get("actions", 0.0))}
        outstanding = [o for o in g.outstanding(child_id, detail=True)]
        g.settle(row["carve_lease"], actual)                 # the unused part of the allowance returns to the parent
        self.registry.set_status(child_id, status, reason, now)
        self.registry.audit(child_id, status, {"reason": reason, "actor": actor, "used": actual,
                                               "unresolved": [o["lease_id"] for o in outstanding]}, now)
        self._emit("CHILD_REVOKED" if status == REVOKED else "CHILD_CLOSED", child_id=child_id, principal=row["principal"],
                   used=actual, unresolved=len(outstanding), reason=reason)
        return {"child_id": child_id, "status": status, "used": actual, "unresolved": [o["lease_id"] for o in outstanding]}

    # ── views ──
    def tree(self, key: str) -> list:
        """Every descendant with its allowance and what it used: the lineage an audit or a Sentinel reads."""
        out = []
        for r in reversed(self.registry.descendants(key)):
            used = self.guard.ledger.used(r["child_id"])
            out.append({"child_id": r["child_id"], "principal": r["principal"], "role": r["role"], "depth": r["depth"],
                        "parent": r["parent_key"], "status": r["status"],
                        "used": {k: used.get(k, 0.0) for k in ("usd", "tokens", "actions")}})
        return out
