"""Counterparty Intelligence — payment behaviour + relationship graph + broker (§9, Table 5). Offline."""
from __future__ import annotations

from agentic_os.integrations.business.contracts import (
    Account, Contact, Customer, Invoice, Order, Provenance, Receivable,
)
from agentic_os.intelligence import resolve_decision_need
from agentic_os.intelligence.families import (
    CounterpartyProvider, CounterpartyRecords, counterparty_registry, counterparty_synthesize,
    payment_behavior, relationship_graph,
)
from runtime_contracts.protocol import Capability, DecisionNeed, ProviderFamily

CUST = "cust:acme"


def _pr(ref):
    return Provenance(provider="erp", provider_ref=ref)


def _inv(ref, status, paid, amount=1000, cur="USD"):
    return Invoice(prov=_pr(ref), customer_ref=CUST, amount_cents=amount, amount_paid_cents=paid,
                   currency=cur, status=status)


def _rec(ref, overdue, outstanding=500):
    return Receivable(prov=_pr(ref), customer_ref=CUST, amount_outstanding_cents=outstanding,
                      currency="USD", days_overdue=overdue)


def _records():
    return CounterpartyRecords(
        invoices=[_inv("i1", "paid", 1000), _inv("i2", "paid", 1000), _inv("i3", "open", 0),
                  _inv("i4", "uncollectible", 0)],
        receivables=[_rec("r1", 12), _rec("r2", 40)],
        customers=[Customer(prov=_pr(CUST), name="Acme", account_ref="acct:1")],
        accounts=[Account(prov=_pr("acct:1"), name="Acme")],
        contacts=[Contact(prov=_pr("ct:1"), account_ref="acct:1", email="a@acme.example")],
        orders=[Order(prov=_pr("o1"), customer_ref=CUST, number="SO-1")],
    )


# ── deterministic ───────────────────────────────────────────────────────────────────────────────────────
def test_payment_behavior_from_own_invoices_and_receivables():
    r = _records()
    pb = payment_behavior(CUST, r.invoices, r.receivables)
    assert pb.n_invoices == 4 and pb.paid_rate == 0.5          # 2 of 4 paid
    assert pb.disputed_count == 1                              # the uncollectible one
    assert pb.avg_days_overdue == 26.0                        # (12 + 40) / 2
    assert pb.total_outstanding_cents == 1000 and pb.currency == "USD"


def test_relationship_graph_links_account_contacts_invoices_orders():
    r = _records()
    g = relationship_graph(CUST, customers=r.customers, accounts=r.accounts, contacts=r.contacts,
                           invoices=r.invoices, orders=r.orders)
    rels = {(e[1]) for e in g.edges}
    assert "has_account" in rels and "has_contact" in rels and "billed_on" in rels and "placed_order" in rels
    assert (CUST, "has_account", "acct:1") in g.edges
    assert ("acct:1", "has_contact", "ct:1") in g.edges


# ── broker path ─────────────────────────────────────────────────────────────────────────────────────────
def _need(cap):
    return DecisionNeed(decision_case_id="dc1", capability=cap, question="?", objective="counterparty_review",
                        subject_refs=(CUST,), tenant="t", min_confidence=0.0, as_of="2026-03-01T00:00:00Z",
                        known_at="2026-03-01T00:00:00Z")


def test_payment_behavior_resolves_through_the_broker():
    res, _ = resolve_decision_need(counterparty_registry(_records()), _need(Capability.PAYMENT_BEHAVIOR),
                                   synthesize=counterparty_synthesize)
    assert res.total_cost == 0.0 and res.provider_receipts[0].provider == "internal.counterparty_intelligence"
    assert "50% paid over 4 invoices" in res.answer and res.metrics["disputed_count"] == 1


def test_relationship_graph_resolves_through_the_broker():
    res, _ = resolve_decision_need(counterparty_registry(_records()), _need(Capability.RELATIONSHIP_GRAPH),
                                   synthesize=counterparty_synthesize)
    assert "relationship edge" in res.answer and len(res.metrics["edges"]) == 7  # acct+contact+4 inv+order


def test_provider_is_cost_zero_and_internal_family():
    p = CounterpartyProvider(_records())
    assert p.family is ProviderFamily.INTERNAL_COMPUTED
    assert p.estimate_cost(_need(Capability.PAYMENT_BEHAVIOR).to_evidence_request()).money == 0.0
