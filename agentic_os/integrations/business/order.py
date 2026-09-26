"""Canonical operational order objects (Intelligence-APIs plan §7, §11).

The operational sales order and its fulfilment spine — distinct from the thin ``commerce.Order`` (an
e-commerce order total). A sales order has lines, each line sources material through one or more purchase
orders (defined in ``supply.py``), and fulfils through a shipment. Same Integration Plane rules: frozen,
``Provenance`` with bitemporal timestamps, ISO dates, evidence-preserving.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Tuple

from .contracts import BusinessObject


@dataclass(frozen=True)
class SalesOrder(BusinessObject):
    """A commitment to a customer. `prov.provider_ref` is the order id lines/shipments reference."""
    KIND: ClassVar[str] = "order.sales_order"
    customer_ref: str = ""
    promised_date: str = ""        # ISO date promised to the customer
    ordered_at: str = ""           # ISO datetime the order was taken
    status: str = ""               # open | in_production | shipped | invoiced | closed | cancelled


@dataclass(frozen=True)
class SalesOrderLine(BusinessObject):
    """A line on a sales order, sourced by zero or more purchase orders (`po_refs`)."""
    KIND: ClassVar[str] = "order.sales_order_line"
    order_ref: str = ""            # SalesOrder.prov.provider_ref
    line_no: str = ""
    part: str = ""
    quantity: float = 0.0
    po_refs: Tuple[str, ...] = ()  # PurchaseOrder.prov.provider_ref(s) that supply this line


@dataclass(frozen=True)
class Shipment(BusinessObject):
    """Fulfilment of a sales order to the customer."""
    KIND: ClassVar[str] = "order.shipment"
    order_ref: str = ""            # SalesOrder.prov.provider_ref
    shipped_date: str = ""         # ISO date shipped
    carrier: str = ""
    status: str = ""               # pending | in_transit | delivered
