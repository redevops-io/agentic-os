"""Execution context — tenant as a per-node resource/action boundary (open-core seam).

The public kernel ships the SINGLE-TENANT invariant: every node runs with
``owner_tenant == execution_tenant == resource_tenant == DEFAULT_TENANT`` and ``authority == "owner"``.
That is a complete, useful, standalone deployment — one organisation, cross-APPLICATION workflows. The
enterprise multi-tenancy plane registers overlays (``set_resource_registry`` / ``set_authority_gate``) that
let those values DIVERGE under explicit delegated authority — N tenants per deployment and governed
cross-TENANT (multi-party) workflows — WITHOUT a second code path: Community is simply the no-divergence
case of these same contracts, so OSS and Enterprise stay API-compatible.

Two independent questions gate every node that touches a resource:
  1. which tenant OWNS this resource?        -> ``ResourceRegistry.resolve(id).owning_tenant``
  2. does this actor/mission have AUTHORITY?  -> ``AuthorityGate.authorize(ctx, resource, capability)``
Same-tenant is allowed by owner authority. Cross-tenant is DENIED by the public default and becomes possible
only when the enterprise ``DelegationAuthority`` mints an explicit, capability-scoped, short-lived credential
lease. Isolation stays fail-closed; cross-tenant is the authorized exception, never a relaxation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from agentic_os.overlays import DEFAULT_TENANT


@dataclass(frozen=True)
class ResourceRef:
    """A named core resource a node acts against. ``owning_tenant`` is RESOURCE tenancy (who owns the data),
    deliberately distinct from the mission's owner tenant. ``credential_ref`` names WHERE a credential/lease
    is resolved (e.g. a Vault path or lease id) — never the secret material itself."""
    resource_id: str
    core: str
    owning_tenant: str = DEFAULT_TENANT
    object_id: str = ""            # the core's tenant object: list id / org id / account id / site / collection
    endpoint: str = ""
    credential_ref: str = ""


@dataclass(frozen=True)
class ExecutionContext:
    """Per-NODE execution context. Single-tenant (Community) default: all three tenants are DEFAULT_TENANT and
    ``authority == "owner"`` — behaviour identical to the pre-tenancy runtime. Enterprise permits divergence
    (a node whose ``resource_tenant`` differs from ``owner_tenant`` under a delegation)."""
    owner_tenant: str = DEFAULT_TENANT       # the tenant that owns the MISSION
    execution_tenant: str = DEFAULT_TENANT   # the tenant this NODE executes as
    resource_tenant: str = DEFAULT_TENANT    # the tenant that OWNS the resource this node touches
    actor_principal: str = "local"
    authority: str = "owner"                 # "owner" | "delegation"
    delegated_by: str | None = None
    authority_chain: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    mission_id: str = ""
    workflow_id: str = ""
    node_id: str = ""

    @property
    def is_cross_tenant(self) -> bool:
        """True when this node touches a resource owned by a tenant other than the mission owner."""
        return self.resource_tenant != self.owner_tenant

    def provenance(self) -> dict:
        """The audit chain carried on every result / evidence / receipt (resource tenancy ≠ workflow tenancy)."""
        return {
            "mission_id": self.mission_id, "workflow_id": self.workflow_id, "node_id": self.node_id,
            "owner_tenant": self.owner_tenant, "execution_tenant": self.execution_tenant,
            "resource_tenant": self.resource_tenant, "actor_principal": self.actor_principal,
            "authority": self.authority, "delegated_by": self.delegated_by,
            "authority_chain": list(self.authority_chain),
        }


def default_execution_context(**overrides) -> ExecutionContext:
    """The single-tenant Community context (owner == execution == resource == DEFAULT_TENANT, owner authority).
    `overrides` lets a caller set mission/node ids without opting into any tenancy divergence."""
    return ExecutionContext(**overrides)


# ════════════════════════════ ResourceRegistry overlay ════════════════════════════
# Q1: which tenant owns a resource, and how is it reached?
class ResourceRegistry(Protocol):
    def resolve(self, resource_id: str) -> ResourceRef: ...


class SingleTenantResourceRegistry:
    """Default: resources are declared in one static map and all belong to DEFAULT_TENANT. An unknown id
    resolves to a default-tenant shell (single-tenant deploys commonly name a resource by its core). The
    enterprise ``EnterpriseResourceRegistry`` registers to resolve arbitrary tenants' resources + lease refs."""

    def __init__(self, resources: "dict[str, ResourceRef] | None" = None) -> None:
        self._resources: dict[str, ResourceRef] = dict(resources or {})

    def register(self, ref: ResourceRef) -> None:
        self._resources[ref.resource_id] = ref

    def resolve(self, resource_id: str) -> ResourceRef:
        ref = self._resources.get(resource_id)
        if ref is not None:
            return ref
        return ResourceRef(resource_id=resource_id, core=resource_id, owning_tenant=DEFAULT_TENANT)


_RESOURCES: ResourceRegistry = SingleTenantResourceRegistry()


def set_resource_registry(registry: ResourceRegistry) -> None:
    global _RESOURCES
    _RESOURCES = registry


def resource_registry() -> ResourceRegistry:
    return _RESOURCES


# ════════════════════════════ AuthorityGate overlay ════════════════════════════
# Q2: does this actor/mission have authority to act on that resource?
@dataclass(frozen=True)
class AuthorityDecision:
    allowed: bool
    reason: str = ""
    lease_ref: "str | None" = None   # enterprise: a short-lived credential-lease id; always None in Community


class AuthorityGate(Protocol):
    def authorize(self, ctx: "ExecutionContext", resource: "ResourceRef", capability: str) -> "AuthorityDecision": ...


class OwnerOnlyAuthority:
    """Default (Community): a node may act on a resource ONLY if that resource is owned by the mission's own
    tenant (owner authority). Cross-tenant access is DENIED — single-tenant mode has no delegation. The
    enterprise ``DelegationAuthority`` registers to permit explicit, capability-scoped cross-tenant leases."""

    def authorize(self, ctx: "ExecutionContext", resource: "ResourceRef", capability: str) -> "AuthorityDecision":
        if resource.owning_tenant == ctx.owner_tenant:
            return AuthorityDecision(True, "owner")
        return AuthorityDecision(
            False,
            f"cross-tenant denied: resource owned by {resource.owning_tenant!r}, mission owner "
            f"{ctx.owner_tenant!r}; requires the enterprise delegation plane",
        )


_AUTHORITY: AuthorityGate = OwnerOnlyAuthority()


def set_authority_gate(gate: AuthorityGate) -> None:
    global _AUTHORITY
    _AUTHORITY = gate


def authority_gate() -> AuthorityGate:
    return _AUTHORITY


# ════════════════════════════ node entry point ════════════════════════════
def resolve_for_node(
    ctx: "ExecutionContext", resource_id: str, capability: str = "",
) -> "tuple[ExecutionContext, ResourceRef, AuthorityDecision]":
    """Resolve a node's acting context against a named resource, answering both gate questions:
      Q1 who owns it  -> `ResourceRegistry.resolve`; the node executes AS the resource's owning tenant;
      Q2 may we act   -> `AuthorityGate.authorize`.
    In Community every resource is DEFAULT_TENANT and the owner authority always allows (no divergence). In
    Enterprise a resource owned by another tenant flips the node to a delegated `execution_tenant`/`authority`
    and the gate returns a lease (or denies). The caller must honour `decision.allowed` (fail closed)."""
    resource = resource_registry().resolve(resource_id)
    cross = resource.owning_tenant != ctx.owner_tenant
    node_ctx = replace(
        ctx,
        execution_tenant=resource.owning_tenant,
        resource_tenant=resource.owning_tenant,
        authority="delegation" if cross else "owner",
    )
    decision = authority_gate().authorize(node_ctx, resource, capability)
    return node_ctx, resource, decision
