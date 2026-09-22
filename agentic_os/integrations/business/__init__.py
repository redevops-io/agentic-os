"""Integration Plane — canonical business contracts (Phase A of the Agentic Apps competitive-gaps plan).

Extends the Integration Plane (not a new top-level package, not the external Agent Gateway) with the
plan's §20 shared business objects so Mission logic reasons about canonical business records rather than
provider JSON. Raw provider payloads stay as evidence; each canonical object retains its provider ref,
evidence refs, and bitemporal timestamps (observed_at / known_at).

Layers added here:
  * ``contracts``   — typed canonical objects (identity/customer · crm · communications · invoice ·
                      payment · support · commerce), money as integer minor units.
  * ``normalize``   — connector ``ProviderResult``/``Observation`` data → canonical objects.
  * ``capabilities``— the business-connector capability registry (VERIFIED / AVAILABLE_UNVERIFIED / … ),
                      the §5 counterpart to the external-agent capability audit, for business SaaS.
  * ``receipts``    — a governed connector write → the canonical ``projects.ActionReceipt`` + an
                      independent verification state (§22 / Strength 5). This wires connector execution
                      into the same receipt/verification the rest of the platform uses.
"""
from .contracts import (
    Account, BusinessObject, Charge, Contact, Customer, Invoice, InvoiceLine, Lead, Message, Opportunity,
    Order, Party, Payment, Provenance, Receivable, Refund, Ticket, now_ms)
from .normalize import normalize, register_normalizer
from .capabilities import (
    ConnectorCapabilityStatus, connector_capability, load_connector_capabilities, provider_capabilities)
from .receipts import VerificationState, receipt_for_step, to_action_receipt, verify_step

__all__ = [
    # contracts
    "Provenance", "BusinessObject", "Party", "Customer", "Account", "Contact", "Lead", "Opportunity",
    "Message", "Invoice", "InvoiceLine", "Payment", "Receivable", "Charge", "Refund", "Ticket", "Order",
    "now_ms",
    # normalization
    "normalize", "register_normalizer",
    # capability registry
    "ConnectorCapabilityStatus", "load_connector_capabilities", "connector_capability",
    "provider_capabilities",
    # receipts + verification
    "VerificationState", "verify_step", "to_action_receipt", "receipt_for_step",
]
