"""Order Intelligence — deterministic order lineage + blockers (Intelligence-APIs plan §7, Table 3).

Business question: will we fulfil this order as promised, what blocks it? The order lineage graph is the
spine — sales order → line → purchase order → supplier confirmation → goods receipt → shipment, every edge
carrying its source and time. `order_lineage` reconstructs that chain (the Phase-1 exit gate "one real
order traceable end-to-end"); `order_blockers` reads it against expected-state rules to name the ranked
blocking dependencies with a causal chain. Both are pure functions over the canonical graph and
leakage-safe (scope the graph with `as_of_ms`), so an answer reconstructs as-of a past decision time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from ...integrations.business.order import SalesOrder, SalesOrderLine, Shipment
from ...integrations.business.supply import GoodsReceipt, PurchaseOrder, SupplierCommitment
from .supplier import delivery_reliability


# ── result types ────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LineageNode:
    kind: str            # sales_order | line | purchase_order | commitment | receipt | shipment
    ref: str
    at: str              # best-available ISO time for this node (order/ship/received/confirmed date)
    source: str          # provider the evidence came from
    status: str
    attaches_to: str     # parent ref (edge), "" for the root


@dataclass(frozen=True)
class OrderLineage:
    order_ref: str
    nodes: tuple[LineageNode, ...]
    stages_present: tuple[str, ...]
    complete: bool       # every line materially received AND shipped


@dataclass(frozen=True)
class Blocker:
    kind: str
    severity: int        # higher = more urgent
    message: str
    causal_chain: tuple[str, ...]    # refs order_ref → line → po → … that explain the block


# ── graph accessor ──────────────────────────────────────────────────────────────────────────────────────
@dataclass
class InMemoryOrderGraph:
    """The canonical order/supply objects, looked up by ref. `as_of_ms` scopes to what was knowable by a
    decision time (known_at, else observed_at) so lineage/blockers never leak a later event."""
    _sales_orders: list[SalesOrder] = field(default_factory=list)
    _lines: list[SalesOrderLine] = field(default_factory=list)
    _pos: list[PurchaseOrder] = field(default_factory=list)
    _commitments: list[SupplierCommitment] = field(default_factory=list)
    _receipts: list[GoodsReceipt] = field(default_factory=list)
    _shipments: list[Shipment] = field(default_factory=list)
    as_of_ms: int = 0

    def _k(self, o) -> bool:
        return not self.as_of_ms or (o.prov.known_at or o.prov.observed_at) <= self.as_of_ms

    def sales_order(self, ref: str) -> Optional[SalesOrder]:
        return next((o for o in self._sales_orders if o.prov.provider_ref == ref and self._k(o)), None)

    def lines(self, order_ref: str) -> list[SalesOrderLine]:
        return [l for l in self._lines if l.order_ref == order_ref and self._k(l)]

    def po(self, ref: str) -> Optional[PurchaseOrder]:
        return next((p for p in self._pos if p.prov.provider_ref == ref and self._k(p)), None)

    def commitments(self, po_ref: str) -> list[SupplierCommitment]:
        return [c for c in self._commitments if c.po_ref == po_ref and self._k(c)]

    def receipts(self, po_ref: str) -> list[GoodsReceipt]:
        return [r for r in self._receipts if r.po_ref == po_ref and self._k(r)]

    def shipment(self, order_ref: str) -> Optional[Shipment]:
        return next((s for s in self._shipments if s.order_ref == order_ref and self._k(s)), None)


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


# ── lineage ─────────────────────────────────────────────────────────────────────────────────────────────
def order_lineage(order_ref: str, g: InMemoryOrderGraph) -> OrderLineage:
    """Reconstruct the order's event/evidence chain, each node carrying its source + time."""
    nodes: list[LineageNode] = []
    so = g.sales_order(order_ref)
    if so is None:
        return OrderLineage(order_ref, (), (), False)
    nodes.append(LineageNode("sales_order", order_ref, so.ordered_at or so.promised_date, so.prov.provider,
                             so.status, ""))
    lines = g.lines(order_ref)
    line_received: list[bool] = []
    for ln in lines:
        lref = ln.prov.provider_ref
        nodes.append(LineageNode("line", lref, "", ln.prov.provider, "", order_ref))
        po_ok = []
        for po_ref in ln.po_refs:
            po = g.po(po_ref)
            if po is not None:
                nodes.append(LineageNode("purchase_order", po_ref, po.ordered_at, po.prov.provider,
                                         po.status, lref))
            for c in g.commitments(po_ref):
                nodes.append(LineageNode("commitment", c.prov.provider_ref, c.committed_at, c.prov.provider,
                                         f"confirmed {c.confirmed_date}", po_ref))
            rec = g.receipts(po_ref)
            for r in rec:
                nodes.append(LineageNode("receipt", r.prov.provider_ref, r.received_date, r.prov.provider,
                                         "received", po_ref))
            po_ok.append(bool(rec))
        # a line with no material dependency is trivially satisfied; otherwise every PO must be received.
        line_received.append(all(po_ok) if ln.po_refs else True)
    sh = g.shipment(order_ref)
    if sh is not None:
        nodes.append(LineageNode("shipment", sh.prov.provider_ref, sh.shipped_date, sh.prov.provider,
                                 sh.status, order_ref))
    complete = bool(lines) and all(line_received) and sh is not None
    stages = tuple(dict.fromkeys(n.kind for n in nodes))   # first-seen order, deduped
    return OrderLineage(order_ref, tuple(nodes), stages, complete)


# ── blockers ────────────────────────────────────────────────────────────────────────────────────────────
def order_blockers(order_ref: str, g: InMemoryOrderGraph) -> list[Blocker]:
    """Ranked blocking dependencies with a causal chain, from expected-state rules over the lineage."""
    so = g.sales_order(order_ref)
    if so is None:
        return []
    promised = _parse_date(so.promised_date)
    blockers: list[Blocker] = []
    lines = g.lines(order_ref)
    all_received = bool(lines)
    for ln in lines:
        lref = ln.prov.provider_ref
        for po_ref in ln.po_refs:
            commits = g.commitments(po_ref)
            receipts = g.receipts(po_ref)
            if receipts:
                continue                                   # this PO is materially satisfied
            all_received = False
            chain = (order_ref, lref, po_ref)
            if not commits:
                blockers.append(Blocker("po_unconfirmed", 2,
                    f"PO {po_ref} for line {lref} is not confirmed by the supplier", chain))
                continue
            c = commits[0]
            committed = _parse_date(c.confirmed_date)
            if promised and committed and committed > promised:
                blockers.append(Blocker("commit_after_promise", 3,
                    f"PO {po_ref}: supplier committed {c.confirmed_date}, after the order's promised "
                    f"date {so.promised_date}", chain + (c.prov.provider_ref,)))
            else:
                blockers.append(Blocker("awaiting_receipt", 2,
                    f"PO {po_ref} for line {lref} is confirmed but not yet received", chain))
    if all_received and g.shipment(order_ref) is None:
        blockers.append(Blocker("ready_to_ship", 1,
            f"order {order_ref} is fully received but not yet shipped", (order_ref,)))
    blockers.sort(key=lambda b: -b.severity)
    return blockers


# ── promise feasibility ─────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PromiseFeasibility:
    order_ref: str
    p_on_time: float               # [0,1] probability the customer promise is met
    earliest_credible_date: str    # ISO date material can credibly be ready to ship ("" = unbounded)
    drivers: tuple[str, ...]       # the main reasons behind the estimate
    confidence: float


def promise_feasibility(order_ref: str, g: InMemoryOrderGraph, supply_source=None, *,
                        ship_lead_days: int = 3) -> PromiseFeasibility:
    """P(on-time) for the customer promise, from the current commitment/receipt state and — when a supply
    event source is given — the suppliers' observed on-time rate (the weakest link governs). Deterministic
    and explainable: a commitment that already lands after the promise makes the promise structurally
    unlikely regardless of reliability; an unconfirmed PO leaves the completion date unbounded."""
    so = g.sales_order(order_ref)
    if so is None:
        return PromiseFeasibility(order_ref, 0.0, "", ("unknown order",), 0.0)
    promised = _parse_date(so.promised_date)

    sh = g.shipment(order_ref)
    if sh is not None:
        shipped = _parse_date(sh.shipped_date)
        on_time = bool(promised and shipped and shipped <= promised)
        return PromiseFeasibility(order_ref, 1.0 if on_time else 0.0, sh.shipped_date,
                                  (f"already shipped {'on time' if on_time else 'late'}",), 1.0)

    # material-ready date = latest per-line completion; None if any dependency is unbounded (unconfirmed PO).
    line_ready: list[Optional[date]] = []
    suppliers: set[str] = set()
    for ln in g.lines(order_ref):
        po_dates: list[Optional[date]] = []
        for po_ref in ln.po_refs:
            po = g.po(po_ref)
            if po is not None:
                suppliers.add(po.supplier_ref)
            recs = g.receipts(po_ref)
            if recs:
                po_dates.append(max(_parse_date(r.received_date) for r in recs))
            else:
                commits = g.commitments(po_ref)
                po_dates.append(_parse_date(commits[0].confirmed_date) if commits else None)
        line_ready.append(None if (not ln.po_refs or any(d is None for d in po_dates)) else max(po_dates))

    if not line_ready or any(d is None for d in line_ready):
        earliest, feasible = "", False
    else:
        ready = max(d for d in line_ready if d is not None)
        earliest_dt = ready + timedelta(days=ship_lead_days)
        earliest = earliest_dt.isoformat()
        feasible = bool(promised and earliest_dt <= promised)

    # supplier prior: weakest supplier's observed on-time rate; a default when we have no history.
    base, confidence = 0.7, 0.5
    if supply_source is not None and suppliers:
        rates, confs = [], []
        for s in suppliers:
            d = delivery_reliability(supply_source.orders(s), supply_source.receipts(s), supplier_ref=s)
            if d.n:
                rates.append(d.on_time_rate)
                confs.append(d.confidence)
        if rates:
            base, confidence = min(rates), min(confs)

    if earliest == "":
        p, drivers = round(0.1 * base, 4), ("an unconfirmed purchase order leaves the completion date unbounded",)
    elif feasible:
        p, drivers = round(base, 4), (f"on track: material ready {earliest} ≤ promised {so.promised_date}",)
    else:
        p, drivers = round(0.1 * base, 4), (f"commitments complete {earliest}, after promised {so.promised_date}",)
    return PromiseFeasibility(order_ref, p, earliest, drivers, round(confidence, 4))
