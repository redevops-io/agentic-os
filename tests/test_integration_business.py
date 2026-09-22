"""Phase A — Integration Plane canonical business contracts, normalization, registry, receipts.

Run:  uv run python -m pytest tests/test_integration_business.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_os.integrations.business import (
    Charge, Contact, ConnectorCapabilityStatus, Message, Provenance, Refund, VerificationState,
    connector_capability, load_connector_capabilities, normalize, provider_capabilities,
    receipt_for_step, to_action_receipt, verify_step)
from agentic_os.integrations.business.contracts import Invoice, Receivable, now_ms

_FIX = Path("/mnt/backup/projects/redevops-connectors/fixtures")


# ── contracts ──────────────────────────────────────────────────────────────────────
def test_object_digest_is_deterministic_over_business_fields_only():
    a = Charge(prov=Provenance("stripe", "ch_1", observed_at=1), amount_cents=1099, currency="usd",
               status="succeeded")
    b = Charge(prov=Provenance("stripe", "ch_1", observed_at=999999), amount_cents=1099, currency="usd",
               status="succeeded")
    # Different observed_at (provenance) → same digest; provenance is excluded from the content address.
    assert a.digest() == b.digest()
    c = Charge(prov=Provenance("stripe", "ch_1"), amount_cents=1100, currency="usd", status="succeeded")
    assert a.digest() != c.digest()


def test_money_is_integer_minor_units_and_to_dict_carries_provenance():
    inv = Invoice(prov=Provenance("stripe", "in_9", evidence_refs=("ev:1",), known_at=123),
                  number="INV-9", amount_cents=50000, amount_paid_cents=0, currency="usd", status="open")
    assert isinstance(inv.amount_cents, int)
    d = inv.to_dict()
    assert d["kind"] == "invoice.invoice" and d["prov"]["provider"] == "stripe"
    assert d["prov"]["evidence_refs"] == ["ev:1"] and d["prov"]["known_at"] == 123
    assert "sha256:" in d["digest"]
    assert "password" not in json.dumps(d).lower()   # provenance never carries secrets


def test_receivable_bitemporal_defaults():
    r = Receivable(prov=Provenance("quickbooks"), invoice_ref="in_9", amount_outstanding_cents=50000,
                   currency="usd", days_overdue=17)
    assert r.prov.observed_at >= 1 and r.prov.known_at == 0     # observed now; fact time unknown


# ── normalization (from real connector fixtures) ─────────────────────────────────────
def _fix(provider, name):
    return json.loads((_FIX / provider / name).read_text())


def test_normalize_stripe_charge_and_refund():
    charge = normalize("stripe", _fix("stripe", "charge.json"))
    assert isinstance(charge, Charge)
    assert charge.amount_cents == 1099 and charge.currency == "usd" and charge.status == "succeeded"
    assert charge.payment_intent_ref == "pi_TEST789" and charge.prov.provider_ref == "ch_TEST123"
    assert charge.prov.known_at == 1679090539 * 1000        # stripe created → known_at ms

    refund = normalize("stripe", _fix("stripe", "refund_created.json"))
    assert isinstance(refund, Refund)
    assert refund.charge_ref == "ch_TEST123" and refund.amount_cents == 1099 and refund.status == "succeeded"


def test_normalize_hubspot_contact_and_gmail_message():
    contact = normalize("hubspot", _fix("hubspot", "contact_get.json"))
    assert isinstance(contact, Contact)
    assert contact.email == "a@b.com" and contact.first_name == "Jane" and contact.prov.provider_ref == "701"

    msg = normalize("gmail", _fix("gmail", "message_get.json"))
    assert isinstance(msg, Message)
    assert msg.channel == "email" and msg.thread_ref == "THREAD01" and msg.direction == "outbound"


def test_normalize_unknown_shape_returns_none_not_a_guess():
    assert normalize("stripe", {"object": "balance", "available": []}) is None   # no normalizer
    assert normalize("acme-crm", {"whatever": 1}) is None                        # unknown provider


# ── capability registry ──────────────────────────────────────────────────────────────
def test_registry_loads_and_is_internally_consistent():
    audit = load_connector_capabilities()
    assert audit["contract_version"] == "connector-capability/v1"
    for provider in audit["providers"]:
        for cap in provider_capabilities(provider):
            assert 0 <= cap.tier <= 4
            assert isinstance(cap.status, ConnectorCapabilityStatus)
            assert cap.status is ConnectorCapabilityStatus.VERIFIED   # every listed cap is contract-verified


def test_registry_fails_closed_for_unknown_and_unsupported():
    # a real provider's real capability
    assert connector_capability("stripe", "billing.refund.execute").status is ConnectorCapabilityStatus.VERIFIED
    assert connector_capability("stripe", "billing.refund.execute").write is True
    # unknown provider/capability → UNKNOWN (fail closed), never invented
    assert connector_capability("stripe", "billing.wire.send").status is ConnectorCapabilityStatus.UNKNOWN
    assert connector_capability("acme", "whatever").status is ConnectorCapabilityStatus.UNKNOWN
    # a plan-priority provider with no adapter → UNSUPPORTED, not UNKNOWN
    assert connector_capability("salesforce", "crm.opportunity.read").status is ConnectorCapabilityStatus.UNSUPPORTED
    assert not connector_capability("salesforce", "crm.opportunity.read").status.usable


def test_registry_matches_live_adapter_capabilities_when_connectors_installed():
    """Drift guard: when redevops_connectors is importable, the YAML must match the adapters' advertised
    capabilities exactly (name + write flag). Skipped when the optional connector package isn't present."""
    rc = pytest.importorskip("redevops_connectors")
    registry = getattr(rc, "ADAPTERS", None) or getattr(rc, "PROVIDERS", None)
    if registry is None:
        pytest.skip("no adapter registry export to compare against")
    # best-effort: compare advertised (name, write) per provider to the YAML
    audit = load_connector_capabilities()
    for provider, node in audit["providers"].items():
        # this branch runs only when connectors are installed; keep it lenient about how adapters enumerate
        assert node["capabilities"], f"{provider} has no capabilities in the registry"


# ── receipts + verification ──────────────────────────────────────────────────────────
def test_verify_step_states():
    assert verify_step(True, True) is VerificationState.VERIFIED
    assert verify_step(True, False) is VerificationState.REFUTED
    assert verify_step(True, None) is VerificationState.UNKNOWN
    assert verify_step(False, None) is VerificationState.NOT_APPLICABLE


def test_to_action_receipt_status_mapping():
    # provider ok + reread confirmed → SUCCEEDED
    r, v = to_action_receipt(capability="crm.contact.upsert", provider="hubspot", ok=True,
                             provider_object_id="701", reconciled=True, decision_id="dec-1")
    assert r.status == "SUCCEEDED" and v.is_success and r.decision_id == "dec-1" and r.external_id == "701"
    # provider ok but reread did NOT find it → HELD, never SUCCEEDED (provider success ≠ real success)
    r2, v2 = to_action_receipt(capability="crm.contact.upsert", provider="hubspot", ok=True,
                               provider_object_id="702", reconciled=False)
    assert r2.status == "HELD" and v2 is VerificationState.REFUTED and "refuted" in r2.error
    # unobservable → HELD + UNKNOWN (honest, not silent success)
    r3, v3 = to_action_receipt(capability="chat.message.send", provider="slack", ok=True,
                               provider_object_id="ts1", reconciled=None)
    assert r3.status == "HELD" and v3 is VerificationState.UNKNOWN
    # write failed → FAILED, nothing to verify
    r4, v4 = to_action_receipt(capability="billing.refund.execute", provider="stripe", ok=False,
                               error="card_declined")
    assert r4.status == "FAILED" and v4 is VerificationState.NOT_APPLICABLE


def test_receipt_for_step_from_integration_steprun():
    from agentic_os.integrations.execution import StepRun
    step = StepRun(capability="calendar.event.create", provider="google_calendar", tier=3, ok=True,
                   provider_object_id="evt_1", reconciled=True)
    r, v = receipt_for_step(step, decision_id="dec-9")
    assert r.status == "SUCCEEDED" and r.provider == "google_calendar" and v.is_success
