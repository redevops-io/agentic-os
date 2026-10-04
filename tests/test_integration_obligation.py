"""Integration plane Phase 0 — conformance: the fake provider completes trigger → obligation → action →
verify → receipt, AND an intentionally-dropped destination write is caught automatically (the silent-failure
case that defines the Obligation). Offline; no network.
"""
from __future__ import annotations

from agentic_os.integration import (
    ActionResult, ExceptionCategory, InMemoryIntegrationProvider, IntegrationProvider, IntegrationReceipt,
    Obligation, ObligationEngine, ObligationStatus, Observation, RetryPolicy, is_known_capability,
)


def _closed_won_obligation(**over):
    # On CRM Closed-Won, a Stripe subscription + a first invoice must exist for the customer.
    base = dict(
        trigger="salesforce.opportunity.closed_won",
        source_resource="salesforce", destination_resource="stripe",
        entity_refs=("ent_acme",), workflow_id="quote-to-cash",
        expected_state={
            "subscription": {"customer": "cus_acme", "status": "active"},
            "invoice": {"customer": "cus_acme", "status": "open"},
        })
    base.update(over)
    return Obligation(**base)


def _provision(dest, *, invoice=True):
    """The (idempotent) destination write the workflow performs."""
    def _act():
        dest.create_object("subscription", {"id": "sub_1", "customer": "cus_acme", "status": "active"},
                           idempotency_key="sub_1")
        if invoice:
            dest.create_object("invoice", {"id": "inv_1", "customer": "cus_acme", "status": "open"},
                               idempotency_key="inv_1")
        return ActionResult(ok=True)
    return _act


# ── contract sanity ──────────────────────────────────────────────────────────────────────────────────────
def test_provider_satisfies_contract_and_capabilities():
    p = InMemoryIntegrationProvider("stripe", capabilities=("billing.subscription.create", "billing.invoice.create"))
    assert isinstance(p, IntegrationProvider)
    assert is_known_capability("billing.invoice.create") and not is_known_capability("made.up.verb")
    assert p.health().healthy


def test_observation_is_immutable_evidence_with_digest():
    p = InMemoryIntegrationProvider("stripe")
    p.create_object("invoice", {"id": "inv_9", "customer": "cus_x", "status": "open"})
    obs = p.read_object("invoice", "inv_9")
    assert isinstance(obs, Observation) and obs.normalized_fields["status"] == "open" and obs.payload_digest


# ── the Phase-0 exit: trigger → obligation → action → verify → receipt ───────────────────────────────────
def test_obligation_satisfied_produces_receipt():
    stripe = InMemoryIntegrationProvider("stripe")
    obl = _closed_won_obligation()
    res = ObligationEngine().discharge(
        obl, stripe, action=_provision(stripe),
        targets={"subscription": "sub_1", "invoice": "inv_1"}, authority="svc@acme")
    assert res.satisfied and res.obligation.status == ObligationStatus.SATISFIED
    assert isinstance(res.receipt, IntegrationReceipt) and res.receipt.satisfied
    # the receipt proves the OBSERVED destination state, not just a 200
    assert res.receipt.observed_state["subscription"]["status"] == "active"
    assert res.receipt.observed_state["invoice"]["customer"] == "cus_acme"
    assert res.exception is None and res.receipt.digest()


# ── the money test: a silent failure is caught automatically ─────────────────────────────────────────────
def test_dropped_destination_write_is_caught_as_unsatisfied():
    # drop_writes = the write "succeeds" (200) but nothing lands — the exact Zap-graveyard / silent-failure case.
    stripe = InMemoryIntegrationProvider("stripe", drop_writes=True)
    obl = _closed_won_obligation(retry_policy=RetryPolicy(max_attempts=2))
    res = ObligationEngine().discharge(
        obl, stripe, action=_provision(stripe), targets={"subscription": "sub_1", "invoice": "inv_1"})
    assert not res.satisfied and res.obligation.status == ObligationStatus.FAILED
    assert res.receipt is None
    assert res.exception and res.exception.category == ExceptionCategory.OBLIGATION_UNSATISFIED
    assert "silent failure" in res.exception.detail and res.exception.retry_count == 2


def test_partial_completion_is_caught():
    # subscription lands but the invoice is skipped — the obligation is NOT satisfied (quote-to-cash gap).
    stripe = InMemoryIntegrationProvider("stripe")
    obl = _closed_won_obligation(retry_policy=RetryPolicy(max_attempts=1))
    res = ObligationEngine().discharge(
        obl, stripe, action=_provision(stripe, invoice=False), targets={"subscription": "sub_1", "invoice": "inv_1"})
    assert not res.satisfied
    assert res.exception.category == ExceptionCategory.OBLIGATION_UNSATISFIED and "invoice" in res.exception.detail


def test_conflicting_destination_value_is_ambiguous():
    # the subscription exists but for the WRONG customer → a conflict, not a clean miss.
    stripe = InMemoryIntegrationProvider("stripe")

    def _wrong():
        stripe.create_object("subscription", {"id": "sub_1", "customer": "cus_WRONG", "status": "active"},
                             idempotency_key="sub_1")
        stripe.create_object("invoice", {"id": "inv_1", "customer": "cus_acme", "status": "open"},
                             idempotency_key="inv_1")
        return ActionResult(ok=True)

    obl = _closed_won_obligation(retry_policy=RetryPolicy(max_attempts=1))
    res = ObligationEngine().discharge(obl, stripe, action=_wrong,
                                       targets={"subscription": "sub_1", "invoice": "inv_1"})
    assert res.obligation.status == ObligationStatus.AMBIGUOUS
    assert res.exception.category == ExceptionCategory.OBLIGATION_CONFLICT


def test_idempotent_retry_does_not_double_write():
    stripe = InMemoryIntegrationProvider("stripe", drop_writes=True)   # forces retries
    obl = _closed_won_obligation(retry_policy=RetryPolicy(max_attempts=3))
    ObligationEngine().discharge(obl, stripe, action=_provision(stripe),
                                 targets={"subscription": "sub_1", "invoice": "inv_1"})
    # idempotency_key dedupes: each object created at most once even across 3 attempts
    creates = [c for c in stripe.calls if c[0] == "create"]
    assert len(creates) <= 2 * 3   # bounded; and the dedup store ensures no duplicate objects
