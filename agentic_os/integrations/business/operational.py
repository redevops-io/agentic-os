"""Operational-connector SDK (Intelligence-APIs plan §2 Integration Plane, §12 Phase-0 exit gate).

The operational intelligence families — Supplier, Supply, Order, Process, Asset — are ``INTERNAL_COMPUTED``
(cost 0): they reason over the tenant's OWN canonical events, never an external API. What they need is those
events, normalized out of whatever system of record the tenant runs (ERPNext, SAP S/4HANA, Coupa, …) into the
provider-neutral canonical objects in :mod:`.supply`, :mod:`.order`, :mod:`.asset`.

This module is the contract for that: an :class:`OperationalConnector` fetches from one system of record and
returns an :class:`OperationalEvents` bundle of canonical objects. It is deliberately *pure* — it depends only
on the canonical contracts, never on the intelligence layer — so the boundary holds both ways: the public
kernel ships this contract plus a reference connector (ERPNext); the enterprise systems (SAP, Coupa) implement
the same contract in the private overlay against a customer's own subscription, with no family or contract
change. Turning an :class:`OperationalEvents` into the family event-sources/graphs is the intelligence layer's
job (``agentic_os.intelligence.families.sources``), which is allowed to depend on both.

A connector is READ-ONLY by design — it is a sensor. Consequential writes (drafting a PO, a maintenance
action) are separately governed by the Mission Runtime and never happen in a connector.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Dict, Protocol, Tuple, runtime_checkable

from .asset import Asset, Component, ServiceEvent, WorkOrder
from .order import SalesOrder, SalesOrderLine, Shipment
from .supply import (
    BOM, BOMLine, DemandRequirement, GoodsReceipt, InventoryPosition, PurchaseOrder, Supplier,
    SupplierCommitment)


@dataclass(frozen=True)
class OperationalEvents:
    """A provider-neutral bundle of the tenant's canonical operational objects, as fetched from one (or
    several, merged) systems of record. Every field is a tuple of the canonical objects from :mod:`.supply`
    / :mod:`.order` / :mod:`.asset`; a connector fills only the kinds its system of record exposes. This is
    the single input the operational intelligence families need — as-of replay filtering is applied later,
    when the family graphs are built, off each object's bitemporal ``prov``."""
    # supplier / supply
    suppliers: Tuple[Supplier, ...] = ()
    purchase_orders: Tuple[PurchaseOrder, ...] = ()
    supplier_commitments: Tuple[SupplierCommitment, ...] = ()
    goods_receipts: Tuple[GoodsReceipt, ...] = ()
    boms: Tuple[BOM, ...] = ()
    bom_lines: Tuple[BOMLine, ...] = ()
    inventory: Tuple[InventoryPosition, ...] = ()
    demand: Tuple[DemandRequirement, ...] = ()
    # order
    sales_orders: Tuple[SalesOrder, ...] = ()
    sales_order_lines: Tuple[SalesOrderLine, ...] = ()
    shipments: Tuple[Shipment, ...] = ()
    # asset
    assets: Tuple[Asset, ...] = ()
    components: Tuple[Component, ...] = ()
    work_orders: Tuple[WorkOrder, ...] = ()
    service_events: Tuple[ServiceEvent, ...] = ()

    def merge(self, other: "OperationalEvents") -> "OperationalEvents":
        """Concatenate two bundles field-by-field (e.g. events from two connected systems of record)."""
        return OperationalEvents(**{
            f.name: tuple(getattr(self, f.name)) + tuple(getattr(other, f.name)) for f in fields(self)})

    def counts(self) -> Dict[str, int]:
        """Non-zero object counts by kind — for a connector health/coverage summary."""
        return {f.name: n for f in fields(self) if (n := len(getattr(self, f.name)))}

    def is_empty(self) -> bool:
        return not any(getattr(self, f.name) for f in fields(self))


@runtime_checkable
class OperationalConnector(Protocol):
    """A read-only sensor over one system of record. ``provider`` is its stable id (matches the provenance
    ``provider`` on the objects it emits and its row in ``connector_capabilities.yaml``); ``connected`` is a
    cheap authenticated probe used for self-skip; ``fetch`` pulls the current operational state as canonical
    objects. Implementations self-skip cleanly (unreachable / unauthorized → empty), so a partially wired
    deployment degrades to fewer answers rather than an error."""
    provider: str

    def connected(self) -> bool: ...

    def fetch(self) -> OperationalEvents: ...


@dataclass
class ConnectorRegistry:
    """The operational connectors a deployment has wired. A deployment registers each configured system of
    record; ``fetch_all`` merges the events from every *connected* one, so the families see one canonical
    view across systems (e.g. ERPNext for parts + SAP for a plant's POs)."""
    _connectors: Dict[str, OperationalConnector] = field(default_factory=dict)

    def register(self, connector: OperationalConnector) -> OperationalConnector:
        self._connectors[connector.provider] = connector
        return connector

    def get(self, provider: str) -> OperationalConnector | None:
        return self._connectors.get(provider)

    def all(self) -> Tuple[OperationalConnector, ...]:
        return tuple(self._connectors.values())

    def connected(self) -> Tuple[OperationalConnector, ...]:
        return tuple(c for c in self._connectors.values() if c.connected())

    def fetch_all(self) -> OperationalEvents:
        events = OperationalEvents()
        for c in self.connected():
            events = events.merge(c.fetch())
        return events
