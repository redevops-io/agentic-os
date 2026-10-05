"""Integration plane (Phase 0) — carry a business obligation across apps and PROVE the final state.

The durable product object is the verified cross-system obligation, not the connector. These contracts + the
Obligation engine are open (community); the SaaS connector fleet, Control Tower, and multi-tenant governance are
the enterprise overlay. See ~/Documents/REDEVOPS_THIRD_PARTY_INTEGRATION_FAILURES_AUDIT_IMPLEMENTATION_PLAN_2026-10-04.md.
"""
from .contracts import (
    CanonicalEntity, EntityType, ExceptionCategory, IntegrationException, IntegrationReceipt, Observation,
    Obligation, ObligationStatus, ReconciliationItem, Resource, ResolutionStatus, RetryPolicy, SyncState,
)
from .cursors import CursorState, CursorStore, detect_missed_events
from .dlq import ExceptionStore, priority_score
from .events import (
    EventInbox, EventRejected, NormalizedEvent, RawEvent, hmac_signature, verify_hmac,
)
from .identity import EntityResolutionPlane, LineageEvent, Match, MergeRefused, resolve, search_matches
from .obligations import DischargeResult, ObligationEngine
from .reconciliation import BalanceTxn, ReconciliationResult, reconcile
from .sync import ConvergeTarget, Report, SyncDecision, converge, plan_sync
from .semantics import (
    MetricDefinition, MetricMapping, MetricReading, SemanticRegistry, default_revenue_registry,
    explain_metric_delta,
)
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
    # reconciliation
    "reconcile", "ReconciliationResult", "BalanceTxn",
    # bidirectional sync (§10)
    "plan_sync", "converge", "Report", "SyncDecision", "ConvergeTarget",
    # durable event ingestion (Phase 1)
    "EventInbox", "RawEvent", "NormalizedEvent", "EventRejected", "hmac_signature", "verify_hmac",
    "CursorStore", "CursorState", "detect_missed_events",
    "ExceptionStore", "priority_score",
    # entity resolution (Phase 2)
    "EntityResolutionPlane", "LineageEvent", "MergeRefused", "Match", "resolve", "search_matches",
    # semantic registry (Phase 2)
    "SemanticRegistry", "MetricDefinition", "MetricMapping", "MetricReading", "explain_metric_delta",
    "default_revenue_registry",
]
