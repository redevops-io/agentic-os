"""Integration plane — synthetic test data (deterministic, offline).

There are no real transactions to test against (Polar.sh/Stripe is wired but unused), and reconciliation must
never run against live money in a test. This generates realistic Stripe-shaped payout fixtures — charges, fees,
refunds, disputes, adjustments, the net payout, and the matching bank deposit — plus deliberately BROKEN
variants to exercise the variance/exception paths. A real connector later feeds the SAME canonical shape, so
the experiments don't change. Reusable across experiments A/B/C.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .provider import InMemoryIntegrationProvider
from .reconciliation import BalanceTxn


@dataclass
class PayoutFixture:
    payout_id: str
    currency: str
    net_amount: int                 # the payout's reported net (minor units)
    bank_deposit: int               # what actually hit the bank
    txns: list                      # list[BalanceTxn]


def synthetic_payout(seed: int = 0, *, charges: int = 5, with_refund: bool = True, with_dispute: bool = False,
                     break_mode: "str | None" = None, currency: str = "usd") -> PayoutFixture:
    """Deterministic payout fixture. ``break_mode``:
      - None             → clean (reconciles)
      - 'missing_txn'    → a fee is dropped from the payout's transaction list (components under-rebuild)
      - 'deposit_mismatch' → the bank deposit is off by a few cents (classic "off by fees" break)
      - 'unknown_type'   → an unclassifiable component appears (schema drift)
    """
    rnd = random.Random(seed)
    pid = f"po_{seed:04d}"
    txns: list[BalanceTxn] = []
    for i in range(charges):
        gross = rnd.randint(1500, 20000)                       # $15–$200
        fee = round(gross * 0.029) + 30                        # Stripe-ish 2.9% + 30c
        txns.append(BalanceTxn(f"txn_{pid}_c{i}", "charge", gross, currency, f"charge {i}"))
        txns.append(BalanceTxn(f"txn_{pid}_f{i}", "fee", fee, currency, f"processing fee {i}"))
    if with_refund:
        txns.append(BalanceTxn(f"txn_{pid}_r0", "refund", rnd.randint(1000, 5000), currency, "customer refund"))
    if with_dispute:
        txns.append(BalanceTxn(f"txn_{pid}_d0", "dispute", rnd.randint(2000, 8000), currency, "chargeback"))
    txns.append(BalanceTxn(f"txn_{pid}_a0", "adjustment", rnd.randint(-200, 200) or 100, currency, "balance adjustment"))

    sign = {"charge": +1, "fee": -1, "refund": -1, "dispute": -1, "adjustment": +1}
    net = sum(sign[t.type] * t.amount for t in txns)

    included = list(txns)
    deposit = net
    if break_mode == "missing_txn":
        included = [t for t in txns if not t.id.endswith("f0")]   # a fee that stops rebuilding to net
    elif break_mode == "deposit_mismatch":
        deposit = net - 137                                        # bank short by $1.37
    elif break_mode == "unknown_type":
        included = txns + [BalanceTxn(f"txn_{pid}_x0", "mystery", 999, currency, "unclassified")]

    return PayoutFixture(payout_id=pid, currency=currency, net_amount=net, bank_deposit=deposit, txns=included)


def seed_providers(fixture: PayoutFixture) -> "tuple[InMemoryIntegrationProvider, InMemoryIntegrationProvider, InMemoryIntegrationProvider]":
    """Load the fixture into (stripe, bank, accounting) in-memory providers as a connector would. Accounting
    starts empty — the experiment posts the clearing entry and verifies it landed."""
    stripe = InMemoryIntegrationProvider("stripe", capabilities=("billing.payment.read", "object.read"))
    bank = InMemoryIntegrationProvider("bank", capabilities=("object.read",))
    accounting = InMemoryIntegrationProvider("accounting", capabilities=("accounting.journal.create", "object.read"))

    stripe.create_object("payout", {"id": fixture.payout_id, "amount": fixture.net_amount,
                                     "currency": fixture.currency, "status": "paid",
                                     "balance_transactions": [t.id for t in fixture.txns]})
    for t in fixture.txns:
        stripe.create_object("balance_transaction", {"id": t.id, "type": t.type, "amount": t.amount,
                                                      "currency": t.currency, "payout": fixture.payout_id,
                                                      "description": t.description})
    bank.create_object("deposit", {"id": f"dep_{fixture.payout_id}", "amount": fixture.bank_deposit,
                                    "currency": fixture.currency, "payout_ref": fixture.payout_id})
    return stripe, bank, accounting
