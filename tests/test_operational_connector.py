"""Operational-connector SDK (plan §2 Integration Plane, §12 Phase-0 exit gate).

Proves the contract end-to-end: a connector's canonical `OperationalEvents` bridge into the operational
family sources and a supplier metric reproduces from those raw events through the same `resolve_decision_need`
path as any provider — the Phase-0 gate "replayable free call with exact cost/evidence" and the Phase-1 gate
"supplier metrics reproduce from raw events". Also proves the ERPNext reference connector maps live doctype
rows to canonical objects (offline, with a stub), and that it satisfies the `OperationalConnector` protocol.

Run:  uv run python -m pytest tests/test_operational_connector.py -q
"""
from __future__ import annotations

from agentic_os.integrations.business import OperationalConnector, OperationalEvents
from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.supply import GoodsReceipt, PurchaseOrder, Supplier
from agentic_os.integrations.erpnext import ErpnextClient
from agentic_os.intelligence import resolve_decision_need
from agentic_os.intelligence.families import (
    operational_bindings, order_graph, supply_events, supplier_synthesize)
from agentic_os.intelligence.families import supplier_registry
from runtime_contracts.protocol import Capability, DecisionNeed

SUP = "sup:acme"


def _po(ref, promised, ordered="2026-01-01T00:00:00Z"):
    return PurchaseOrder(prov=Provenance("erpnext", ref), supplier_ref=SUP, site="A", part="P-1",
                         quantity=100.0, promised_date=promised, ordered_at=ordered, status="open")


def _rcpt(po_ref, received):
    return GoodsReceipt(prov=Provenance("erpnext", f"r-{po_ref}"), po_ref=po_ref, supplier_ref=SUP,
                        received_date=received, quantity=100.0)


def _events(n=5, *, late=False):
    recv = "2026-01-17" if late else "2026-01-10"       # promised 2026-01-10
    return OperationalEvents(
        suppliers=(Supplier(prov=Provenance("erpnext", SUP), name=SUP),),
        purchase_orders=tuple(_po(f"po{i}", "2026-01-10") for i in range(n)),
        goods_receipts=tuple(_rcpt(f"po{i}", recv) for i in range(n)))


def _need(cap=Capability.DELIVERY_RELIABILITY):
    return DecisionNeed(decision_case_id="dc1", capability=cap, question="how reliable is acme?",
                        objective="supplier_review", subject_refs=(SUP,), tenant="t", min_confidence=0.2,
                        as_of="2026-01-20T00:00:00Z", known_at="2026-01-20T00:00:00Z")


# ── OperationalEvents container ───────────────────────────────────────────────────────────────────────────
def test_events_merge_counts_and_empty():
    assert OperationalEvents().is_empty()
    merged = _events(2).merge(OperationalEvents(purchase_orders=(_po("extra", "2026-02-01"),)))
    assert merged.counts()["purchase_orders"] == 3 and merged.counts()["goods_receipts"] == 2
    assert not merged.is_empty()


# ── bridge → family source, resolved through the broker ───────────────────────────────────────────────────
def test_supplier_metric_reproduces_from_connector_events():
    src = supply_events(_events(5))                        # OperationalEvents → SupplyEventSource
    assert len(list(src.orders(SUP))) == 5 and len(list(src.receipts(SUP))) == 5
    res, _ = resolve_decision_need(supplier_registry(src), _need(), synthesize=supplier_synthesize)
    assert "OTIF 100%" in res.answer and res.metrics["otif"] == 1.0 and res.metrics["n"] == 5
    assert res.total_cost == 0.0                           # internal-computed, free


def test_late_receipts_drop_otif():
    res, _ = resolve_decision_need(supplier_registry(supply_events(_events(5, late=True))), _need(),
                                   synthesize=supplier_synthesize)
    assert res.metrics["otif"] == 0.0                      # all 5 received a week late


def test_operational_bindings_cover_the_families():
    bindings = operational_bindings(_events(3))
    caps = {c for reg, _ in bindings for p in reg.all() for c in p.capabilities()}
    # one bundle wires supplier + order + supply + asset families
    assert Capability.DELIVERY_RELIABILITY in caps and Capability.PROMISE_FEASIBILITY in caps
    assert Capability.SHORTAGE_RISK in caps and Capability.FAILURE_RISK in caps


def test_as_of_replay_filters_later_events():
    # an order knowable only after the decision's as-of must not be seen (leakage-safe replay)
    future = PurchaseOrder(prov=Provenance("erpnext", "future", known_at=4_000_000_000_000),
                           supplier_ref=SUP, part="P-1", promised_date="2027-01-01", status="open")
    bundle = _events(2).merge(OperationalEvents(purchase_orders=(future,)))
    as_of = 2_000_000_000_000                                    # after the po0/po1 observation, before 'future'
    src = supply_events(bundle, as_of_ms=as_of)
    refs = {po.prov.provider_ref for po in src.orders(SUP)}
    assert "future" not in refs and refs == {"po0", "po1"}       # only the knowable orders
    assert order_graph(bundle, as_of_ms=as_of).as_of_ms == as_of


# ── ERPNext reference connector (offline stub) ────────────────────────────────────────────────────────────
class _StubErpnext(ErpnextClient):
    def __init__(self):
        super().__init__(base_url="http://stub", api_key="k", api_secret="s")

    def connected(self) -> bool:
        return True

    def get_list(self, doctype, fields, filters=None, limit=0):
        return {
            "Purchase Order": [{"name": "PO-001", "supplier": "ACME", "transaction_date": "2026-01-01",
                                "schedule_date": "2026-01-10", "status": "To Receive", "set_warehouse": "Main"}],
            "Purchase Order Item": [{"parent": "PO-001", "item_code": "P-1", "qty": 100.0, "uom": "Nos"}],
            "Purchase Receipt": [{"name": "PR-001", "supplier": "ACME", "posting_date": "2026-01-12",
                                  "status": "Completed", "is_return": 0}],
            "Purchase Receipt Item": [{"parent": "PR-001", "purchase_order": "PO-001", "item_code": "P-1",
                                       "received_qty": 100.0, "qty": 100.0}],
        }.get(doctype, [])


def test_erpnext_maps_doctypes_to_canonical_objects():
    events = _StubErpnext().operational_events()
    assert events.counts() == {"suppliers": 1, "purchase_orders": 1, "goods_receipts": 1}
    po = events.purchase_orders[0]
    assert po.supplier_ref == "ACME" and po.part == "P-1" and po.quantity == 100.0
    assert po.promised_date == "2026-01-10" and po.status == "confirmed"   # "To Receive" → confirmed
    assert po.prov.provider == "erpnext" and po.prov.provider_ref == "PO-001"
    rcpt = events.goods_receipts[0]
    assert rcpt.po_ref == "PO-001" and rcpt.received_date == "2026-01-12" and rcpt.quality_ok


def test_erpnext_satisfies_the_operational_connector_protocol():
    stub = _StubErpnext()
    assert isinstance(stub, OperationalConnector) and stub.provider == "erpnext"
    assert not stub.fetch().is_empty()


def test_erpnext_self_skips_when_disconnected():
    client = ErpnextClient(base_url="http://unreachable.invalid", api_key="k", api_secret="s")
    assert client.operational_events().is_empty()          # connected() probe fails → empty, no raise
