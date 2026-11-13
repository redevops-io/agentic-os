"""Assemble a synthetic deployment: gateway with a durable authority guard, the approvals inbox, a child-agent spawner, the bridge and its stores.
Calling ``build_env`` again with the same ``workdir`` and ``crm`` models a process restart (the CRM is the external system and is kept)."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agentic_os.agent_gateway import AgentGateway, InboxApprovalStore
from agentic_os.mission.authority import AuthorityGuard, MemoryEnvelopeStore, MemoryLedger, SqliteEnvelopeStore, SqliteLedger
from agentic_os.mission.children import ChildRegistry, ChildSpawner
from agentic_os.mission.store import EventStore, MemoryEventStore

from ..callback_auth import CallbackClient, CallbackOrigin, Peer, PeerRegistry
from ..lineage import ChildScopePolicy, LineageManager
from ..rap_gateway import RapGateway
from ..store import OutboxStore
from . import capabilities as caps
from .mock_crm import MockCrm

TENANT = "acme-demo"
PRINCIPAL = "infinity-runtime"
AUDIENCE = "redevops-rap-bridge-demo"
KEY_ID = "runtime-key-1"
SECRET = b"demo-" * 8                              # synthetic placeholder; a deployment reads its own from a secret manager
CHILD_SECRET = b"kid-" * 9


class DemoAuthorizer:
    """Stands in for the identity plane. Revocable mid-run."""

    def __init__(self, grants=("crm.read", "crm.write")):
        self.grants = set(grants)

    def __call__(self, principal: Any, permission: str) -> bool:
        return permission in self.grants


@dataclass
class Env:
    workdir: str
    crm: MockCrm
    journal: Any
    inbox: InboxApprovalStore
    authorizer: DemoAuthorizer
    envelopes: Any
    ledger: Any
    guard: AuthorityGuard
    spawner: ChildSpawner
    gateway: AgentGateway
    store: OutboxStore
    peers: PeerRegistry
    peer: Peer
    lineage: LineageManager
    bridge: RapGateway
    clock: Callable[[], float]
    tenant: str = TENANT


def build_env(workdir: str, *, crm: Optional[MockCrm] = None, journal: Optional[Any] = None, inbox: Optional[Any] = None,
              authorizer: Optional[DemoAuthorizer] = None, clock: Callable[[], float] = time.time, tenant: str = TENANT, durable: bool = True,
              callback_origins=(), grants: Optional[tuple] = None, child_policy: Optional[ChildScopePolicy] = None, with_spawner: bool = True,
              callbacks: Optional[CallbackClient] = None, peer_kwargs: Optional[dict] = None, bridge_kwargs: Optional[dict] = None,
              operations: Optional[dict] = None) -> Env:
    os.makedirs(workdir, exist_ok=True)
    crm = crm or MockCrm(os.path.join(workdir, "crm.json"))
    journal = journal if journal is not None else (EventStore(os.path.join(workdir, "rap-journal.jsonl")) if durable else MemoryEventStore())
    inbox = inbox if inbox is not None else InboxApprovalStore(clock=clock)
    authorizer = authorizer or DemoAuthorizer()
    envelopes = SqliteEnvelopeStore(os.path.join(workdir, "envelopes.sqlite")) if durable else MemoryEnvelopeStore()
    ledger = SqliteLedger(os.path.join(workdir, "usage.sqlite")) if durable else MemoryLedger()
    if envelopes.get(f"{tenant}:{PRINCIPAL}") is None:
        envelopes.put(caps.default_envelope(tenant, PRINCIPAL, now=clock(), grants=grants))
    guard = AuthorityGuard(envelopes, ledger, clock=clock)
    spawner = ChildSpawner(guard, ChildRegistry(os.path.join(workdir, "children.sqlite")), CHILD_SECRET, clock=clock, lease_ttl_s=600.0) if with_spawner else None
    gateway = AgentGateway(registry=caps.build_registry(crm), authorize=authorizer, approvals=inbox, authority=guard,
                           child_credentials=spawner.verifier() if spawner else None)
    store = OutboxStore(os.path.join(workdir, "rap-outbox.sqlite"), clock=clock)
    peers = PeerRegistry(AUDIENCE, clock=clock)
    peer = peers.add(Peer(KEY_ID, SECRET, tenant, PRINCIPAL, AUDIENCE, callback_origins=tuple(callback_origins), **(peer_kwargs or {})))
    ops = operations or caps.default_operations()
    lineage = LineageManager(store, guard, spawner, {**ops}, child_policy or ChildScopePolicy())
    bridge = RapGateway(gateway, ops, store, journal, peers, lineage=lineage, callbacks=callbacks, clock=clock, **(bridge_kwargs or {}))
    lineage.operations = bridge.operations
    return Env(workdir, crm, journal, inbox, authorizer, envelopes, ledger, guard, spawner, gateway, store, peers, peer, lineage, bridge, clock, tenant)


def loopback_origin(port: int) -> CallbackOrigin:
    return CallbackOrigin(f"http://127.0.0.1:{port}", allow_insecure_loopback=True)
