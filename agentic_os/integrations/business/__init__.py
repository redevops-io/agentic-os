"""Integration Plane — canonical business contracts + the governed-Runtime substrate (PUBLIC).

This package answers *what contracts does a decision Runtime obey?* — canonical business objects, connector
normalization, a capability registry, governed writes into the platform's ``ActionReceipt`` + verification,
and cross-app governed missions. Raw provider payloads stay as evidence; each canonical object retains its
provider ref, evidence refs, and bitemporal timestamps.

It does **not** contain the proprietary answer to *how does ReDevOps turn verified operational experience
into better future decisions?* — the decision-learning engine (experience attribution, pattern/lesson
induction, applicability, safe-Learn abstention, policy optimization, cross-domain transfer, the benchmark
worlds and their goldens) lives in the private ``decision-intelligence`` package, behind the
:class:`DecisionIntelligence` interface defined here. Public depends only on that interface; the private
engine depends on these public contracts, never the reverse.
"""
from .contracts import (
    Account, BusinessObject, Charge, Contact, Customer, Invoice, InvoiceLine, Lead, Message, Opportunity,
    Order, Party, Payment, Provenance, Receivable, Refund, Ticket, now_ms)
from .normalize import normalize, register_normalizer
from .capabilities import (
    ConnectorCapabilityStatus, connector_capability, load_connector_capabilities, provider_capabilities)
from .receipts import VerificationState, receipt_for_step, to_action_receipt, verify_step
from .runner import (
    GovernedMissionResult, GovernedStep, canonical_evidence, govern_run, receipts_for_run)
from .missions import (
    FollowupProposal, ReceivableCandidate, investigate_receivables, propose_followups)
from .decision_intelligence import (
    Admissibility, DecisionIntelligence, DeterministicDecisionIntelligence, NoLearningDecisionIntelligence,
    experience_eligible)

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
    # governed run → canonical evidence + receipts
    "canonical_evidence", "receipts_for_run", "govern_run", "GovernedMissionResult", "GovernedStep",
    # cross-app governed Receivables Mission (deterministic; public example)
    "investigate_receivables", "propose_followups", "ReceivableCandidate", "FollowupProposal",
    # Decision Intelligence interface boundary (implementations are private)
    "DecisionIntelligence", "NoLearningDecisionIntelligence", "DeterministicDecisionIntelligence",
    "Admissibility", "experience_eligible",
]
