"""Bridge: a connector's :class:`OperationalEvents` → the operational family event-sources / graphs.

The operational-connector SDK (:mod:`agentic_os.integrations.business.operational`) is pure — it produces
canonical objects and knows nothing about the intelligence families. This module is the seam that turns one
:class:`OperationalEvents` bundle into the in-memory sources the Supplier / Order / Supply / Asset families
resolve against, and into ready-to-bind ``(registry, synthesize)`` pairs. It lives in the intelligence layer
because it depends on both sides; the connectors themselves never do.

``as_of_ms`` flows straight through to the family graphs, which already leakage-filter each object off its
bitemporal ``prov`` — so a decision replayed as-of a past time never sees a later event.
"""
from __future__ import annotations

from typing import List, Tuple

from ...integrations.business.operational import OperationalEvents
from .asset_broker import AssetGraph, asset_registry, asset_synthesize
from .order import InMemoryOrderGraph
from .order_broker import order_registry, order_synthesize
from .supplier_broker import InMemorySupplyEvents, supplier_registry, supplier_synthesize
from .supply_broker import SupplyGraph, supply_registry, supply_synthesize

_OPEN_PO = ("open", "confirmed")


def supply_events(events: OperationalEvents, as_of_ms: int = 0) -> InMemorySupplyEvents:
    """The supplier-metrics event source (orders / receipts / commitments, indexed by supplier)."""
    return InMemorySupplyEvents(list(events.purchase_orders), list(events.goods_receipts),
                                list(events.supplier_commitments), as_of_ms)


def order_graph(events: OperationalEvents, as_of_ms: int = 0) -> InMemoryOrderGraph:
    """The order lineage/blocker/feasibility graph (sales orders → lines → POs → commitments → receipts →
    shipments)."""
    return InMemoryOrderGraph(list(events.sales_orders), list(events.sales_order_lines),
                              list(events.purchase_orders), list(events.supplier_commitments),
                              list(events.goods_receipts), list(events.shipments), as_of_ms)


def supply_graph(events: OperationalEvents, as_of_ms: int = 0) -> SupplyGraph:
    """The supply graph (inventory / open supply / demand / BOM), with the supplier on-time prior wired in
    from the same events."""
    open_supply = [po for po in events.purchase_orders if not po.status or po.status in _OPEN_PO]
    return SupplyGraph(inventory=list(events.inventory), open_supply=open_supply,
                       demand=list(events.demand), bom_lines=list(events.bom_lines),
                       order_lines=list(events.sales_order_lines), sales_orders=list(events.sales_orders),
                       supply_source=supply_events(events, as_of_ms), as_of_ms=as_of_ms)


def asset_graph(events: OperationalEvents, as_of_ms: int = 0) -> AssetGraph:
    """The asset graph (assets / components / work orders / service events / spares inventory)."""
    return AssetGraph(assets=list(events.assets), components=list(events.components),
                      work_orders=list(events.work_orders), service_events=list(events.service_events),
                      inventory=list(events.inventory), as_of_ms=as_of_ms)


def operational_bindings(events: OperationalEvents, as_of_ms: int = 0) -> List[Tuple[object, object]]:
    """The ``(registry, synthesize)`` pairs for every operational family, ready to feed one at a time to an
    ``IntelligenceService.bind(*pair)``. One connector bundle → the whole operational broker surface. The
    supplier event-source is shared into the order graph so order feasibility uses the same on-time prior."""
    src = supply_events(events, as_of_ms)
    return [
        (supplier_registry(src), supplier_synthesize),
        (order_registry(order_graph(events, as_of_ms), src), order_synthesize),
        (supply_registry(supply_graph(events, as_of_ms)), supply_synthesize),
        (asset_registry(asset_graph(events, as_of_ms)), asset_synthesize),
    ]
