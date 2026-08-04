"""Public extension seams + their standalone defaults — the overlay points the enterprise planes plug into.

Every private plane attaches here by *registering* an implementation; the public runtime ships a working
default for each, so a **public-only deploy runs standalone**: single-tenant, deterministic, deny-by-default,
with no discovery sources, static (non-learning) policy, and a no-op context fabric. Same pattern as
``merge.set_default_advisor`` / ``topology.set_default_optimizer`` — the public core imports nothing private;
it only offers these hooks, and an ``agentic-os-enterprise`` overlay calls the ``set_*``/``register_*``
functions on import to swap the operational implementations in.

Contract version: ``overlays/v8`` — the extension-seam set (the Discovery / ExecutionPlan era). Additive
within that lineage (new seams, new optional methods); a later whitepaper that redefines a seam bumps it.

(Retrieval contracts live in the retriever layer — ``redevops-rag`` / the Context Runtime — which is already
public; this module covers the mission/runtime planes: discovery, learning, identity, tenancy, fabric.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

CONTRACT_VERSION = "overlays/v8"


# ════════════════════════════ Discovery (propose work) ════════════════════════════
class DiscoverySource(Protocol):
    """Where missions come from besides humans and the SDK. The full Discovery Runtime (detectors,
    correlation, hypotheses, prioritization) is an enterprise plane that registers as a source; the public
    default is *no sources* — public missions are human- or SDK-authored."""

    def proposals(self) -> Iterable[Any]: ...


_DISCOVERY_SOURCES: list[DiscoverySource] = []


def register_discovery_source(source: DiscoverySource) -> None:
    _DISCOVERY_SOURCES.append(source)


def discovery_sources() -> list[DiscoverySource]:
    return list(_DISCOVERY_SOURCES)


# ════════════════════════════ Learning (outcome / reward hooks) ════════════════════════════
@dataclass
class Outcome:
    """The reward signal a mission emits. The public runtime records it; the enterprise self-learning
    plane (bandits, off-policy eval, policy promotion) consumes it. ``policy_version`` pins which policy
    produced the decision, so a learned decision is never mistaken for the static default."""
    mission_id: str
    reward: float
    policy_version: str = "static/v0"
    context: dict = field(default_factory=dict)


class RewardSink(Protocol):
    def record(self, outcome: Outcome) -> None: ...


class LocalRewardLog:
    """Default: records outcomes, learns nothing (static policy). The enterprise learner registers to
    actually improve policy from these."""

    def __init__(self) -> None:
        self.outcomes: list[Outcome] = []

    def record(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)


_REWARD_SINK: RewardSink = LocalRewardLog()


def set_reward_sink(sink: RewardSink) -> None:
    global _REWARD_SINK
    _REWARD_SINK = sink


def reward_sink() -> RewardSink:
    return _REWARD_SINK


# ════════════════════════════ Identity & access ════════════════════════════
@dataclass(frozen=True)
class Principal:
    id: str
    kind: str = "user"                       # user | service | workload
    roles: tuple[str, ...] = ()
    tenant: str = "default"


class IdentityProvider(Protocol):
    def authenticate(self, token: str) -> "Principal | None": ...
    def authorize(self, principal: "Principal", capability: str) -> bool: ...


class LocalIdentity:
    """Default: a single local principal with **deny-by-default** grants — a capability runs only if it was
    explicitly granted. The enterprise plane (SSO/OIDC, ABAC, break-glass, tenant isolation, access ledger)
    registers to replace this."""

    def __init__(self, principal: "Principal | None" = None, grants: Iterable[str] | None = None) -> None:
        self.principal = principal or Principal("local", "user", ("operator",))
        self.grants = set(grants or ())

    def authenticate(self, token: str) -> "Principal | None":
        return self.principal                # local: one principal, no federation

    def authorize(self, principal: "Principal", capability: str) -> bool:
        return capability in self.grants     # deny-by-default

    def grant(self, capability: str) -> None:
        self.grants.add(capability)


_IDENTITY: IdentityProvider = LocalIdentity()


def set_identity_provider(provider: IdentityProvider) -> None:
    global _IDENTITY
    _IDENTITY = provider


def identity_provider() -> IdentityProvider:
    return _IDENTITY


# ════════════════════════════ Tenancy (single-tenant default) ════════════════════════════
DEFAULT_TENANT = "default"


class TenancyProvider(Protocol):
    def current_tenant(self) -> str: ...


class SingleTenant:
    """Default: one tenant. The enterprise multi-tenancy plane (isolation, billing boundaries, migrations)
    registers to replace this."""

    def current_tenant(self) -> str:
        return DEFAULT_TENANT


_TENANCY: TenancyProvider = SingleTenant()


def set_tenancy_provider(provider: TenancyProvider) -> None:
    global _TENANCY
    _TENANCY = provider


def tenancy_provider() -> TenancyProvider:
    return _TENANCY


# ════════════════════════════ Context Fabric (no-op default) ════════════════════════════
class ContextFabric(Protocol):
    """Physical KV-cache lifecycle — identity / residency / reuse / prefetch / QoS. Public default is a
    no-op passthrough (correct, just unoptimized); the enterprise Context Fabric overlay registers the
    real provider-aware cache lifecycle."""

    def reuse_key(self, context_id: str) -> "str | None": ...
    def on_materialize(self, context_id: str, size_tokens: int) -> None: ...


class NoOpFabric:
    def reuse_key(self, context_id: str) -> "str | None":
        return None                          # no reuse — correct behaviour, no optimization

    def on_materialize(self, context_id: str, size_tokens: int) -> None:
        pass


_FABRIC: ContextFabric = NoOpFabric()


def set_context_fabric(fabric: ContextFabric) -> None:
    global _FABRIC
    _FABRIC = fabric


def context_fabric() -> ContextFabric:
    return _FABRIC


# ════════════════════════════ self-test / golden fixture ════════════════════════════
def _demo() -> int:
    checks: list[tuple[str, bool]] = []

    # a public-only deploy: the standalone defaults
    checks.append(("no discovery sources by default (human/SDK-authored)", discovery_sources() == []))
    checks.append(("identity is deny-by-default", identity_provider().authorize(Principal("local"), "cap.deploy") is False))
    checks.append(("single tenant by default", tenancy_provider().current_tenant() == DEFAULT_TENANT))
    checks.append(("context fabric is a no-op (no reuse)", context_fabric().reuse_key("cv-1") is None))
    reward_sink().record(Outcome("m1", 1.0))
    checks.append(("reward sink records locally (static policy)", isinstance(reward_sink(), LocalRewardLog)))

    # overlays register and take effect
    class EntDiscovery:
        def proposals(self): return ["p1"]
    register_discovery_source(EntDiscovery())
    checks.append(("enterprise discovery source registers", len(discovery_sources()) == 1))

    local = LocalIdentity(grants=["cap.plan"])
    set_identity_provider(local)
    checks.append(("an explicit grant authorizes; others denied",
                   identity_provider().authorize(Principal("local"), "cap.plan") and
                   not identity_provider().authorize(Principal("local"), "cap.deploy")))
    set_identity_provider(LocalIdentity())   # restore

    class EntFabric:
        def reuse_key(self, cid): return "warm-" + cid
        def on_materialize(self, cid, n): pass
    set_context_fabric(EntFabric())
    checks.append(("enterprise fabric overlay provides reuse", context_fabric().reuse_key("cv-1") == "warm-cv-1"))
    set_context_fabric(NoOpFabric())         # restore

    for name, ok in checks:
        print(f"  check {name} = {ok}")
    passed = sum(1 for _, ok in checks if ok)
    print(f"RESULT {passed}/{len(checks)}  (overlay seams {CONTRACT_VERSION})")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    import sys
    sys.exit(_demo())
