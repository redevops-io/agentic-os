"""Mission lineage <-> Infinity thread lineage, and the authority of a child thread.

An Infinity agent can spawn child threads; the invocation carries ``group_id`` (the thread) and ``thread_ancestors`` (root to parent). The
kernel already has the concept this maps onto: a child agent is an identity with its own attenuated envelope, its own credential and a
place in a lineage (``agentic_os.mission.children.ChildSpawner``). Nothing new is invented here; this module only decides WHEN to spawn:

* a root thread acts under the peer's standing envelope ``{tenant}:{principal}`` (installed by the owner, never by the runtime);
* a child thread is spawned from its parent's envelope with ``ChildSpawner.spawn``: grants only for operations the owner marked
  ``child_allowed`` and that the parent's grants already cover (with a call limit), a smaller budget, a lifetime no longer than the parent's
  remaining time, and a credential the gateway verifies. The credential stays inside the bridge; the runtime never sees it;
* the lineage the runtime reports is recorded on first sight and cannot change afterwards; a different ``thread_ancestors`` for a known thread is refused;
* ``/close_thread`` ends the child (and everything below it) and returns what it did not use to its parent. Revoking or narrowing a
  parent denies every descendant at the next decision (``AuthorityGuard.chain_denial``).

If no spawner is configured, child threads are refused: they never inherit the parent's authority unattenuated.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from typing import Dict, Mapping, Optional, Tuple

from agentic_os.mission.authority import AutonomyBudget, Grant
from agentic_os.mission.children import SpawnRefused

from .callback_auth import Peer
from .contracts import Reason
from .operation_binding import OperationSpec
from .store import OutboxStore


class LineageRefused(Exception):
    def __init__(self, code: str, reason: str = Reason.LINEAGE_REFUSED):
        super().__init__(code)
        self.code, self.reason = code, reason


@dataclass(frozen=True)
class ChildScopePolicy:
    ttl_s: float = 3600.0                    # never longer than the parent's remaining time (attenuate enforces it)
    max_actions: int = 20
    usd: float = 0.0
    tokens: int = 0
    max_children: int = 0                    # a child may not spawn grandchildren unless the owner says so
    max_depth: int = 0
    max_calls_per_grant: int = 10
    may_delegate_children: bool = False      # may a child thread spawn its own children (Infinity threads nest); off unless the owner says so

    def budget(self, ttl_s: float) -> AutonomyBudget:
        return AutonomyBudget(usd=self.usd, tokens=self.tokens, wall_clock_s=ttl_s, max_actions=self.max_actions,
                              max_children=self.max_children, max_depth=self.max_depth, max_parallel=1)


@dataclass(frozen=True)
class Authority:
    """Who the gateway sees: the envelope key, the principal id, and (for a child) the credential that proves it."""
    key: str
    subject: str
    credential: Optional[str]
    child_id: str = ""


def derive_child_grants(parent_grants: Tuple[Grant, ...], operations: Mapping[str, OperationSpec], policy: ChildScopePolicy) -> Tuple[Grant, ...]:
    """One grant per child-allowed gateway capability that some parent grant covers; each is the parent's grant narrowed to that exact
    capability with a call limit, so ``Grant.covers`` (and therefore ``attenuate``) accepts it and a child never exceeds its parent."""
    out = []
    for op in operations.values():
        if op.local or not op.child_allowed:
            continue
        g = next((g for g in parent_grants if fnmatchcase(op.capability, g.capability)), None)
        if g is None:
            continue
        calls = policy.max_calls_per_grant if g.max_calls is None else min(int(g.max_calls), policy.max_calls_per_grant)
        out.append(replace(g, capability=op.capability, max_calls=calls))
    return tuple(out)


class LineageManager:
    def __init__(self, store: OutboxStore, guard, spawner, operations: Mapping[str, OperationSpec], policy: Optional[ChildScopePolicy] = None):
        self.store, self.guard, self.spawner, self.operations = store, guard, spawner, operations
        self.policy = policy or ChildScopePolicy()
        self._creds: Dict[Tuple[str, str, str], str] = {}
        self._lock = threading.RLock()

    @staticmethod
    def root_key(peer: Peer) -> str:
        return f"{peer.tenant}:{peer.principal}"

    def ensure(self, peer: Peer, group_id: str, ancestors: Tuple[str, ...]) -> None:
        """Record (and, for a child, spawn) the thread and every ancestor not yet known. Raises :class:`LineageRefused`."""
        chain = list(ancestors) + [group_id]
        with self._lock:
            for i, gid in enumerate(chain):
                self._ensure_one(peer, gid, tuple(chain[:i]))

    def _ensure_one(self, peer: Peer, gid: str, anc: Tuple[str, ...]) -> None:
        have = self.store.lineage(peer.tenant, peer.key_id, gid)
        if have is not None:
            if tuple(json.loads(have["ancestors_json"])) != anc:
                raise LineageRefused("lineage_changed")
            if have["closed"]:
                raise LineageRefused("thread_closed")
            return
        if not anc:
            self.store.put_lineage({"tenant": peer.tenant, "key_id": peer.key_id, "group_id": gid, "parent_group_id": "",
                                    "ancestors_json": json.dumps([]), "authority_key": self.root_key(peer), "child_id": "", "depth": 0})
            return
        if self.spawner is None:
            raise LineageRefused("children_not_enabled", Reason.SPAWN_REFUSED)
        parent = self.store.lineage(peer.tenant, peer.key_id, anc[-1])
        assert parent is not None
        penv = self.guard.envelopes.get(parent["authority_key"])
        if penv is None or penv.tenant != peer.tenant:
            raise LineageRefused("no_parent_authority", Reason.SPAWN_REFUSED)
        remaining = penv.expires_at - self.guard.clock()
        if remaining <= 2.0:
            raise LineageRefused("parent_expired", Reason.SPAWN_REFUSED)
        ttl = min(self.policy.ttl_s, remaining - 1.0)                          # a child never outlives its parent
        grants = derive_child_grants(penv.grants, self.operations, self.policy)
        if not grants:
            raise LineageRefused("no_delegable_operations", Reason.SPAWN_REFUSED)
        try:
            child = self.spawner.spawn(parent["authority_key"], peer.tenant, role=f"infinity-thread", goal=f"infinity thread {gid}"[:200],
                                       grants=grants, budget=self.policy.budget(ttl), ttl_s=ttl,
                                       may_delegate=self.policy.may_delegate_children, client=peer.key_id)
        except SpawnRefused as e:
            raise LineageRefused(e.code, Reason.SPAWN_REFUSED) from None
        if not self.store.put_lineage({"tenant": peer.tenant, "key_id": peer.key_id, "group_id": gid, "parent_group_id": anc[-1],
                                       "ancestors_json": json.dumps(list(anc)), "authority_key": child.key, "child_id": child.child_id,
                                       "depth": child.depth}):
            self.spawner.close(child.child_id, "lineage already recorded")     # lost a race: keep exactly one child per thread
            return
        self._creds[(peer.tenant, peer.key_id, gid)] = child.credential

    def authority(self, peer: Peer, group_id: str) -> Authority:
        """The authority to present to the gateway for a call on this thread. Heartbeats the child (the OWNER's liveness statement, made only
        after the peer authenticated and the lineage checked), and renews the credential if this process restarted and lost it."""
        row = self.store.lineage(peer.tenant, peer.key_id, group_id)
        if row is None or row["closed"]:
            raise LineageRefused("unknown_thread")
        if not row["child_id"]:
            return Authority(row["authority_key"], peer.principal, None)
        with self._lock:
            self.spawner.heartbeat(row["child_id"])
            k = (peer.tenant, peer.key_id, group_id)
            cred = self._creds.get(k)
            if cred is None:
                try:
                    cred = self.spawner.renew_credential(row["child_id"], client=peer.key_id).credential
                except SpawnRefused:
                    raise LineageRefused("child_not_active", Reason.SPAWN_REFUSED) from None
                self._creds[k] = cred
            row_p = self.spawner.registry.get(row["child_id"])
            return Authority(row["authority_key"], row_p["principal"], cred, row["child_id"])

    def close_thread(self, peer: Peer, group_id: str) -> int:
        """End a thread's authority (and its descendants'). Idempotent; an unknown thread is a no-op. Returns how many lineage rows closed."""
        n = 0
        with self._lock:
            targets = [group_id] + [c["group_id"] for c in self.store.lineage_children(peer.tenant, peer.key_id, group_id)]
            for gid in targets:
                row = self.store.lineage(peer.tenant, peer.key_id, gid)
                if row is None or row["closed"]:
                    continue
                if row["child_id"] and self.spawner is not None:
                    try:
                        self.spawner.close(row["child_id"], "infinity thread closed")
                    except SpawnRefused:
                        pass
                self.store.update_lineage(peer.tenant, peer.key_id, gid, closed=1)
                self._creds.pop((peer.tenant, peer.key_id, gid), None)
                n += 1
        return n
