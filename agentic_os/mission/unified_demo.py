"""Unified two-domain Mission — the P2 vertical slice of the unified-desktop redesign.

One governed Mission crosses two GENUINELY separate domains — Revenue/CRM (Twenty) and Support (Chatwoot) —
through the same seams the real headless apps use: capability operators (``operator_sdk``), the world
``AdapterRegistry`` for provider-neutral cores, and the authority-scoped ``CredentialBroker`` for every
credential. It proves the product hypothesis: the apps behave as one agent-operated system, driven by a
Mission + approval, with NO app-specific UI and NO provider vocabulary leaking to the caller.

Two invariants this module is built to demonstrate:
  1. A consequential cross-domain action suspends at a human gate and resumes durably across a restart.
  2. No domain credential is ever visible outside the broker boundary — the Twenty/Chatwoot tokens are
     redeemed as ephemeral material at the capability boundary and never enter mission state, events,
     the receipt, or evidence. (The unified UI therefore only ever sees "CRM — Connected", never a key.)

Plan:  read the CRM account  ─▶  (approval gate)  ─▶  upsert the contact into Support.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

from runtime_contracts import (
    AuthorityContext,
    CredentialRequirement,
    EnvironmentSecretStore,
    LocalCredentialBroker,
    PrincipalRef,
    SecretRef,
)

from .executor import Executor
from .operator_sdk import LocalOperatorClient, Operator, capability
from .registry import CapabilityRegistry
from .runtime import MissionRuntime
from .store import EventStore
from .types import ExecutionIntent, IntentStep

# Least-privilege grants the mission runs under (the compiler fails closed without them). These are the
# capability permissions; the AuthorityContext scope below must additionally cover them.
GRANTS: List[str] = ["crm:read", "support:write"]

# Which env-backed secret each capability resolves through the broker (dev-grade store; swap for Vault
# later with no change here). The NAME is the key the handler reads out of its `secrets` dict.
_CRED = {
    "crm.read_account": ("twenty", "TWENTY_API_KEY", ("crm:read",)),
    "support.contact.upsert": ("chatwoot", "CHATWOOT_API_TOKEN", ("support:write",)),
}


def _slug(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in (s or "")).strip("-") or "acct"


# ── domain handlers: provider-neutral world adapters, credential injected by the broker ────────────────

def _crm_read(inputs: dict, secrets: dict) -> dict:
    """Revenue/CRM read. Resolves the Twenty credential through the broker (never env) and reports the
    account through the world adapter — REAL-LIVE when Twenty answers, honest in-memory otherwise."""
    from agentic_os.world.adapters import InMemoryAdapter, TwentyCrmAdapter
    account = inputs.get("account") or "Beacon Industrial Parks"
    mat = (secrets or {}).get("twenty")
    connected, realism = False, InMemoryAdapter("twenty").realism
    if mat is not None:
        base = os.environ.get("TWENTY_BASE_URL") or os.environ.get("TWENTY_URL") or "http://twenty:3000"
        adapter = TwentyCrmAdapter(base=base, token=mat.bytes().decode("utf-8", "replace"))
        if adapter.available():
            connected, realism = True, adapter.realism
    return {"account": account, "connected": connected, "realism": realism,
            "brief": f"{account}: active re-roof program; storm-season buying signal"}


def _support_upsert(inputs: dict, secrets: dict) -> dict:
    """Support write (the consequential, approval-gated action). Resolves the Chatwoot credential through
    the broker and upserts the account's contact via the world adapter; in-memory projection if the core
    is unreachable — never a raw-env path."""
    from agentic_os.world.adapters import ChatwootAdapter, CoreUnavailable, InMemoryAdapter
    from agentic_os.world.objects import CanonicalObject
    crm = inputs.get("account") or {}
    account = (crm.get("account") if isinstance(crm, dict) else crm) or "Beacon Industrial Parks"
    obj = CanonicalObject(canonical_id=_slug(account), kind="contact", label=account,
                          attributes={"account": account}, provenance="unified-demo")
    mat = (secrets or {}).get("chatwoot")
    if mat is not None:
        base = os.environ.get("CHATWOOT_BASE_URL") or "http://chatwoot:3000"
        adapter = ChatwootAdapter(base=base, token=mat.bytes().decode("utf-8", "replace"))
        try:
            return {"native_id": adapter.upsert(obj), "realism": adapter.realism,
                    "account": account, "connected": True}
        except CoreUnavailable:
            pass
    mem = InMemoryAdapter("chatwoot")
    return {"native_id": mem.upsert(obj), "realism": mem.realism, "account": account, "connected": False}


# ── operators + registry ───────────────────────────────────────────────────────────────────────────────

def build_unified_operators() -> Dict[str, Operator]:
    crm = Operator("agentic-crm", [
        capability("crm.read_account", _crm_read, provides=["account"],
                   inputs={"account": "string"}, outputs={"account": "string", "brief": "string"},
                   permissions=["crm:read"], required_authority=["crm:read"],
                   data_classifications=["crm"], usd=0.0, latency_ms=200),
    ])
    support = Operator("agentic-support", [
        capability("support.contact.upsert", _support_upsert, provides=["contact_synced"],
                   inputs={"account": "string"}, outputs={"native_id": "string"},
                   side_effecting=True, approval_required=True, deterministic=False,
                   undo="support.contact.archive",
                   permissions=["support:write"], required_authority=["support:write"],
                   data_classifications=["support"], usd=0.0, latency_ms=300, estimated_value="high"),
    ])
    return {op.name: op for op in (crm, support)}


class UnifiedTwoDomainPlanner:
    """Emits the two logical steps directly (no NL parser, no template): read the CRM account, then sync
    its contact into Support. The compiler maps each step's outcome to the capability that ``provides`` it."""

    def plan(self, mission_id: str, goal: str, context: dict) -> ExecutionIntent:
        return ExecutionIntent(mission_id=mission_id, rationale="revenue→support cross-domain pilot", steps=[
            IntentStep(outcome="account", need="read the CRM account"),
            IntentStep(outcome="contact_synced", need="sync the account contact into support",
                       inputs_from=["account"]),
        ])


# ── credential/authority seams (open-source has no CapabilitySpec.secrets→requirement map; supply it) ───

def _credentials_for(node) -> Tuple[CredentialRequirement, ...]:
    spec = _CRED.get(node.capability)
    if not spec:
        return ()
    name, env_key, scopes = spec
    return (CredentialRequirement(name=name, required_scopes=scopes,
                                  secret_ref=SecretRef(provider="env", key=env_key),
                                  production_broker_required=False, max_ttl_seconds=120),)


def _authority_for(node) -> Tuple[str, ...]:
    spec = _CRED.get(node.capability)
    return (spec[2] if spec else ())  # required scopes double as the required authority for this demo


def build_unified_runtime(store_path: str, *, tenant: str = "Meridian Wealth Management"):
    """Assemble a MissionRuntime whose Executor carries the broker + a service AuthorityContext scoped to
    exactly crm:read + support:write, over a JSONL EventStore at ``store_path`` (so it survives restart)."""
    operators = build_unified_operators()
    reg = CapabilityRegistry()
    for op in operators.values():
        reg.register(op.manifest)
    authority = AuthorityContext(
        authority_id="unified-demo",
        principal=PrincipalRef(id="unified-demo", kind="service", tenant=tenant),
        purpose="cross-domain-pilot", scope=("crm:read", "support:write"))
    broker = LocalCredentialBroker(EnvironmentSecretStore())
    executor = Executor(LocalOperatorClient(operators), authority=authority, broker=broker,
                        credentials_for=_credentials_for, authority_for=_authority_for)
    store = EventStore(path=store_path)
    rt = MissionRuntime(reg, executor, store=store, planner=UnifiedTwoDomainPlanner())
    return rt, reg


def seed_mission(rt, *, goal: str = "Sync the pilot account from Revenue into Support",
                 account: str = "Beacon Industrial Parks"):
    """Create + run the two-domain mission to the approval gate. Returns the Mission (state WAITING_HUMAN)."""
    m = rt.create_mission(goal, policy_refs=list(GRANTS))
    rt.run(m.id)
    return m


def connections_view(rt) -> Dict[str, Any]:
    """The credential-INVISIBLE view the unified UI is allowed to see: connection status per domain, and
    never a token. Presence of the env-backed secret is reported as connected/degraded — the value stays
    with the broker."""
    return {"connections": [
        {"domain": "CRM", "provider": "Twenty", "connected": bool(os.environ.get("TWENTY_API_KEY")),
         "credential": "broker-managed"},
        {"domain": "Support", "provider": "Chatwoot",
         "connected": bool(os.environ.get("CHATWOOT_API_TOKEN")), "credential": "broker-managed"},
    ]}
