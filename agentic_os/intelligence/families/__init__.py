"""Intelligence families (Intelligence-APIs plan §5–§10).

Each family is a thin set of deterministic/statistical functions over the canonical business graph,
served to the broker as a capability. Supplier is the first (Phase 1): reliability metrics computed
straight from the tenant's own PO / confirmation / receipt events — no external provider needed, and
reproducible as-of a past decision time (leakage-safe).
"""
from .supplier import (
    ConfirmationReliability,
    DeliveryReliability,
    confirmation_reliability,
    delivery_reliability,
)
from .supplier_broker import (
    InMemorySupplyEvents,
    SupplierMetricsProvider,
    SupplyEventSource,
    supplier_registry,
    supplier_synthesize,
)
from .order import (
    Blocker,
    InMemoryOrderGraph,
    LineageNode,
    OrderLineage,
    order_blockers,
    order_lineage,
)

__all__ = [
    "DeliveryReliability",
    "ConfirmationReliability",
    "delivery_reliability",
    "confirmation_reliability",
    "SupplyEventSource",
    "InMemorySupplyEvents",
    "SupplierMetricsProvider",
    "supplier_registry",
    "supplier_synthesize",
    "OrderLineage",
    "LineageNode",
    "Blocker",
    "InMemoryOrderGraph",
    "order_lineage",
    "order_blockers",
]
