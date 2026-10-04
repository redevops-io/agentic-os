"""Integration plane (Phase 0) — carry a business obligation across apps and PROVE the final state.

The durable product object is the verified cross-system obligation, not the connector. These contracts + the
Obligation engine are open (community); the SaaS connector fleet, Control Tower, and multi-tenant governance are
the enterprise overlay. See ~/Documents/REDEVOPS_THIRD_PARTY_INTEGRATION_FAILURES_AUDIT_IMPLEMENTATION_PLAN_2026-10-04.md.
"""
from .contracts import (
    CanonicalEntity, EntityType, ExceptionCategory, IntegrationException, IntegrationReceipt, Observation,
    Obligation, ObligationStatus, ReconciliationItem, Resource, ResolutionStatus, RetryPolicy, SyncState,
)
from .obligations import DischargeResult, ObligationEngine
from .provider import (
    CAPABILITIES, ActionResult, InMemoryIntegrationProvider, IntegrationError, IntegrationErrorCode,
    IntegrationProvider, ProviderHealth, is_known_capability,
)

__all__ = [
    # contracts
    "Resource", "Observation", "CanonicalEntity", "Obligation", "ObligationStatus", "RetryPolicy",
    "ReconciliationItem", "SyncState", "IntegrationException", "IntegrationReceipt", "ExceptionCategory",
    "EntityType", "ResolutionStatus",
    # provider
    "IntegrationProvider", "InMemoryIntegrationProvider", "IntegrationError", "IntegrationErrorCode",
    "ActionResult", "ProviderHealth", "CAPABILITIES", "is_known_capability",
    # engine
    "ObligationEngine", "DischargeResult",
]
