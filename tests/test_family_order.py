"""Order Intelligence — lineage + blockers (Intelligence-APIs plan §7, Phase-1 exit gate). Offline.

Pins: one real order is traceable end-to-end through the canonical graph; blockers are named deterministically
with a causal chain and ranked; and both are leakage-safe as-of a past decision time.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.order import SalesOrder, SalesOrderLine, Shipment
from agentic_os.integrations.business.supply import GoodsReceipt, PurchaseOrder, SupplierCommitment
from agentic_os.intelligence.families import InMemoryOrderGraph, order_blockers, order_lineage

SUP = "sup:acme"


def _prov(ref, known_at=0):
    return Provenance(provider="erp", provider_ref=ref, known_at=known_at)


def _so(ref="so1", promised="2026-02-01"):
    return SalesOrder(prov=_prov(ref), customer_ref="cust:1", promised_date=promised,
                      ordered_at="2026-01-01T00:00:00Z", status="open")


def _line(ref, order_ref, po_refs, part="P-1"):
    return SalesOrderLine(prov=_prov(ref), order_ref=order_ref, line_no=ref, part=part, quantity=10.0,
                          po_refs=tuple(po_refs))


def _po(ref):
    return PurchaseOrder(prov=_prov(ref), supplier_ref=SUP, site="A", part="P-1", quantity=10.0,
                         promised_date="2026-01-20", ordered_at="2026-01-02T00:00:00Z", status="open")


def _commit(po_ref, confirmed="2026-01-20"):
    return SupplierCommitment(prov=_prov(f"c-{po_ref}"), po_ref=po_ref, supplier_ref=SUP,
                              confirmed_date=confirmed, committed_at="2026-01-03T00:00:00Z")


def _rcpt(po_ref, received="2026-01-19", known_at=0):
    return GoodsReceipt(prov=_prov(f"r-{po_ref}", known_at=known_at), po_ref=po_ref, supplier_ref=SUP,
                        received_date=received, quantity=10.0)


def _ship(order_ref="so1", shipped="2026-01-28"):
    return Shipment(prov=_prov(f"ship-{order_ref}"), order_ref=order_ref, shipped_date=shipped,
                    carrier="dhl", status="in_transit")


def _fulfilled_graph(**kw):
    """A fully fulfilled order: 2 lines, each with a confirmed + received PO, then shipped. Any field can
    be overridden via kw (replacing the default, not appended)."""
    defaults = dict(
        _sales_orders=[_so()],
        _lines=[_line("L1", "so1", ["po1"]), _line("L2", "so1", ["po2"])],
        _pos=[_po("po1"), _po("po2")],
        _commitments=[_commit("po1"), _commit("po2")],
        _receipts=[_rcpt("po1"), _rcpt("po2")],
        _shipments=[_ship()],
    )
    defaults.update(kw)
    return InMemoryOrderGraph(**defaults)


def test_one_order_traceable_end_to_end():
    lin = order_lineage("so1", _fulfilled_graph())
    kinds = [n.kind for n in lin.nodes]
    assert kinds[0] == "sales_order"
    for stage in ("sales_order", "line", "purchase_order", "commitment", "receipt", "shipment"):
        assert stage in lin.stages_present
    assert lin.complete is True
    # every non-root node records the edge it attaches to (the graph is connected).
    assert all(n.attaches_to for n in lin.nodes if n.kind != "sales_order")
    # a receipt node carries its source + time.
    rcpt = next(n for n in lin.nodes if n.kind == "receipt")
    assert rcpt.source == "erp" and rcpt.at == "2026-01-19"


def test_fulfilled_order_has_no_blockers():
    assert order_blockers("so1", _fulfilled_graph()) == []


def test_missing_receipt_blocks_and_names_the_causal_chain():
    g = _fulfilled_graph(_receipts=[_rcpt("po1")])       # po2 not received
    blocks = order_blockers("so1", g)
    assert len(blocks) == 1
    b = blocks[0]
    assert b.kind == "awaiting_receipt" and b.causal_chain == ("so1", "L2", "po2")


def test_commitment_after_promise_outranks_awaiting_receipt():
    # po1 committed AFTER the order's promised date (real risk); po2 merely awaiting receipt.
    g = _fulfilled_graph(_commitments=[_commit("po1", confirmed="2026-02-10"), _commit("po2")],
                         _receipts=[])
    blocks = order_blockers("so1", g)
    assert [b.kind for b in blocks] == ["commit_after_promise", "awaiting_receipt"]   # ranked by severity
    assert blocks[0].causal_chain == ("so1", "L1", "po1", "c-po1")


def test_unconfirmed_po_is_flagged():
    g = _fulfilled_graph(_commitments=[_commit("po1")], _receipts=[_rcpt("po1")])   # po2 no commit, no receipt
    blocks = order_blockers("so1", g)
    assert [b.kind for b in blocks] == ["po_unconfirmed"]
    assert blocks[0].causal_chain == ("so1", "L2", "po2")


def test_ready_to_ship_when_received_but_not_shipped():
    g = _fulfilled_graph(_shipments=[])                  # everything received, no shipment
    blocks = order_blockers("so1", g)
    assert [b.kind for b in blocks] == ["ready_to_ship"]


def test_as_of_scoped_graph_is_leakage_safe():
    # Explicit fact times: everything is knowable by t=1000 except po2's receipt (t=5000). As-of t=3000
    # the order is not complete and po2 still blocks — no leakage of the later receipt.
    def p(ref):
        return _prov(ref, known_at=1000)
    g = InMemoryOrderGraph(
        _sales_orders=[SalesOrder(prov=p("so1"), customer_ref="c", promised_date="2026-02-01",
                                  ordered_at="2026-01-01T00:00:00Z", status="open")],
        _lines=[SalesOrderLine(prov=p("L1"), order_ref="so1", line_no="L1", part="P", quantity=10.0,
                               po_refs=("po1",)),
                SalesOrderLine(prov=p("L2"), order_ref="so1", line_no="L2", part="P", quantity=10.0,
                               po_refs=("po2",))],
        _pos=[_po_kn("po1"), _po_kn("po2")],
        _commitments=[SupplierCommitment(prov=p("c-po1"), po_ref="po1", supplier_ref=SUP,
                                         confirmed_date="2026-01-20", committed_at="2026-01-03T00:00:00Z"),
                      SupplierCommitment(prov=p("c-po2"), po_ref="po2", supplier_ref=SUP,
                                         confirmed_date="2026-01-20", committed_at="2026-01-03T00:00:00Z")],
        _receipts=[GoodsReceipt(prov=p("r-po1"), po_ref="po1", supplier_ref=SUP, received_date="2026-01-19",
                                quantity=10.0),
                   GoodsReceipt(prov=_prov("r-po2", known_at=5000), po_ref="po2", supplier_ref=SUP,
                                received_date="2026-01-19", quantity=10.0)],
        _shipments=[], as_of_ms=3000)
    lin = order_lineage("so1", g)
    assert lin.complete is False
    assert [b.kind for b in order_blockers("so1", g)] == ["awaiting_receipt"]


def _po_kn(ref):
    return PurchaseOrder(prov=_prov(ref, known_at=1000), supplier_ref=SUP, site="A", part="P", quantity=10.0,
                         promised_date="2026-01-20", ordered_at="2026-01-02T00:00:00Z", status="open")


def test_unknown_order_is_empty_not_an_error():
    assert order_lineage("nope", _fulfilled_graph()).nodes == ()
    assert order_blockers("nope", _fulfilled_graph()) == []
