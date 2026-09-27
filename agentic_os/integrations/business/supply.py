"""Canonical supply-chain business objects (Intelligence-APIs plan §11 canonical-graph additions).

The provider-neutral objects the Supplier / Supply / Order intelligence families compute over — purchase
orders, the supplier's own confirmations, and goods receipts. They follow the same rules as the rest of
the Integration Plane: frozen, evidence-preserving, every object carries :class:`Provenance` with
bitemporal ``observed_at`` / ``known_at`` (so a metric can be reconstructed as-of a past decision time),
money in integer minor units, dates as ISO strings, raw payloads referenced not inlined.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Mapping

from .contracts import BusinessObject


@dataclass(frozen=True)
class Supplier(BusinessObject):
    """An organization the tenant buys from. `site` is a specific ship-from plant/DC when known."""
    KIND: ClassVar[str] = "supply.supplier"
    name: str = ""
    site: str = ""
    category: str = ""
    external_ids: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PurchaseOrder(BusinessObject):
    """A commitment to buy. Its `prov.provider_ref` is the PO id that commitments/receipts reference."""
    KIND: ClassVar[str] = "supply.purchase_order"
    supplier_ref: str = ""
    site: str = ""
    part: str = ""
    quantity: float = 0.0
    unit: str = ""
    promised_date: str = ""        # ISO date the goods are promised by
    ordered_at: str = ""           # ISO datetime the PO was placed
    status: str = ""               # open | confirmed | received | cancelled


@dataclass(frozen=True)
class SupplierCommitment(BusinessObject):
    """The supplier's own confirmation of a PO — the promise the runtime later checks against reality."""
    KIND: ClassVar[str] = "supply.commitment"
    po_ref: str = ""               # PurchaseOrder.prov.provider_ref
    supplier_ref: str = ""
    confirmed_date: str = ""       # ISO date the supplier committed to
    committed_at: str = ""         # ISO datetime the confirmation arrived
    promise_change_count: int = 0  # how many times the confirmed date moved


@dataclass(frozen=True)
class GoodsReceipt(BusinessObject):
    """Reality: what actually arrived, when. The verification event supplier metrics are computed from."""
    KIND: ClassVar[str] = "supply.receipt"
    po_ref: str = ""               # PurchaseOrder.prov.provider_ref
    supplier_ref: str = ""
    received_date: str = ""        # ISO date goods were received
    quantity: float = 0.0
    quality_ok: bool = True


# ── bill of materials / inventory / demand (Supply Intelligence — §11) ────────────────────────────────────
@dataclass(frozen=True)
class BOM(BusinessObject):
    """A bill-of-materials header: a revision of how a parent part is built from components."""
    KIND: ClassVar[str] = "supply.bom"
    parent_part: str = ""
    revision: str = ""
    status: str = ""               # active | draft | obsolete


@dataclass(frozen=True)
class BOMLine(BusinessObject):
    """One component of a parent part, with the quantity required per unit of the parent. A component may
    itself be a parent (multi-level BOM). `is_substitute` marks an approved alternate for `component_part`."""
    KIND: ClassVar[str] = "supply.bom_line"
    parent_part: str = ""
    component_part: str = ""
    quantity_per: float = 1.0
    is_substitute: bool = False
    substitute_for: str = ""       # the primary component this is an approved alternate to


@dataclass(frozen=True)
class InventoryPosition(BusinessObject):
    """On-hand stock of a part at a site. `allocated` is already committed to other demand."""
    KIND: ClassVar[str] = "supply.inventory"
    part: str = ""
    site: str = ""
    on_hand: float = 0.0
    allocated: float = 0.0


@dataclass(frozen=True)
class DemandRequirement(BusinessObject):
    """A dated demand for a part (a line of the demand plan) — what must be available, where and when."""
    KIND: ClassVar[str] = "supply.demand"
    part: str = ""
    site: str = ""
    need_date: str = ""            # ISO date the quantity is required by
    quantity: float = 0.0
    source_ref: str = ""           # e.g. the sales-order line driving this demand
