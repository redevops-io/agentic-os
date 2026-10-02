"""Phase-0 open-core tenancy contracts: single-tenant default is a no-op; the overlay seam permits divergence."""
from __future__ import annotations

import agentic_os.execution_context as ec
from agentic_os.execution_context import (
    AuthorityDecision,
    ExecutionContext,
    OwnerOnlyAuthority,
    ResourceRef,
    SingleTenantResourceRegistry,
    default_execution_context,
    resolve_for_node,
    resource_registry,
    set_authority_gate,
    set_resource_registry,
)
from agentic_os.overlays import DEFAULT_TENANT


def test_community_default_is_single_tenant_no_divergence():
    ctx = default_execution_context(mission_id="m1", node_id="n1")
    assert ctx.owner_tenant == ctx.execution_tenant == ctx.resource_tenant == DEFAULT_TENANT
    assert ctx.authority == "owner"
    assert ctx.is_cross_tenant is False


def test_single_tenant_registry_resolves_known_and_unknown():
    reg = SingleTenantResourceRegistry({"listmonk:acme": ResourceRef("listmonk:acme", "listmonk", object_id="7")})
    known = reg.resolve("listmonk:acme")
    assert known.object_id == "7" and known.owning_tenant == DEFAULT_TENANT
    unknown = reg.resolve("lago")  # single-tenant deploys often name a resource by its core
    assert unknown.core == "lago" and unknown.owning_tenant == DEFAULT_TENANT


def test_owner_only_authority_allows_same_tenant_denies_cross():
    gate = OwnerOnlyAuthority()
    ctx = default_execution_context()
    same = ResourceRef("r", "listmonk", owning_tenant=DEFAULT_TENANT)
    other = ResourceRef("r", "listmonk", owning_tenant="acme")
    assert gate.authorize(ctx, same, "send").allowed is True
    d = gate.authorize(ctx, other, "send")
    assert d.allowed is False and "cross-tenant" in d.reason


def test_resolve_for_node_community_path_allows_default_resource():
    # default registry: unknown id → default-tenant shell → owner authority allows
    ctx = default_execution_context(mission_id="m1")
    node_ctx, resource, decision = resolve_for_node(ctx, "listmonk", capability="send")
    assert decision.allowed is True and decision.lease_ref is None
    assert node_ctx.is_cross_tenant is False and node_ctx.authority == "owner"
    assert resource.owning_tenant == DEFAULT_TENANT


def test_overlay_seam_permits_enterprise_divergence_and_delegated_lease():
    """Proves Community and Enterprise are one codebase + a divergence switch: registering enterprise-shaped
    overlays lets owner/execution/resource diverge and a cross-tenant node obtain a lease — same contracts."""

    class EntRegistry:  # resources owned by arbitrary tenants
        def resolve(self, resource_id: str) -> ResourceRef:
            tenant = resource_id.split(":", 1)[1] if ":" in resource_id else DEFAULT_TENANT
            return ResourceRef(resource_id, resource_id.split(":", 1)[0], owning_tenant=tenant, object_id="x")

    class DelegationAuthority:  # permits cross-tenant under an explicit, capability-scoped lease
        def authorize(self, ctx, resource, capability):
            if resource.owning_tenant == ctx.owner_tenant:
                return AuthorityDecision(True, "owner")
            if capability in ctx.capabilities:  # delegation covers this capability
                return AuthorityDecision(True, "delegation", lease_ref=f"lease:{resource.owning_tenant}:{capability}")
            return AuthorityDecision(False, "delegation does not cover capability")

    prev_reg, prev_gate = resource_registry(), ec.authority_gate()
    try:
        set_resource_registry(EntRegistry())
        set_authority_gate(DelegationAuthority())

        # mission owned by A, node acts on B's resource under a delegation covering "reconcile"
        ctx = ExecutionContext(owner_tenant="A", actor_principal="svc@A", capabilities=("reconcile",),
                               mission_id="m2", node_id="n2")
        node_ctx, resource, decision = resolve_for_node(ctx, "lago:B", capability="reconcile")
        assert resource.owning_tenant == "B"
        assert node_ctx.owner_tenant == "A" and node_ctx.execution_tenant == "B" and node_ctx.resource_tenant == "B"
        assert node_ctx.is_cross_tenant is True and node_ctx.authority == "delegation"
        assert decision.allowed is True and decision.lease_ref == "lease:B:reconcile"

        # a capability the delegation does NOT cover is refused (fail closed)
        bad = resolve_for_node(ctx, "lago:B", capability="delete")[2]
        assert bad.allowed is False

        # provenance carries the full cross-party chain
        p = node_ctx.provenance()
        assert p["owner_tenant"] == "A" and p["resource_tenant"] == "B" and p["authority"] == "delegation"
    finally:
        set_resource_registry(prev_reg)
        set_authority_gate(prev_gate)


def test_default_overlays_restored_outside_enterprise():
    # after the enterprise test restores, the process is single-tenant again
    assert isinstance(resource_registry(), SingleTenantResourceRegistry)
    # the default registry maps every id to DEFAULT_TENANT, so check cross-tenant denial with an explicit ref
    d = ec.authority_gate().authorize(
        default_execution_context(), ResourceRef("r", "lago", owning_tenant="B"), "x"
    )
    assert d.allowed is False  # restored owner-only gate denies cross-tenant
