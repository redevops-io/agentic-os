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
from .runner import (
    GovernedMissionResult, GovernedStep, canonical_evidence, govern_run, receipts_for_run)
from .missions import (
    FollowupProposal, ReceivableCandidate, investigate_receivables, propose_followups)
from .decisions import (
    AccountFeatures, Candidate, CandidateSet, DecisionContext, DecisionExperience, DecisionPoint,
    DecisionProposal, DecisionRecord, DeterministicReceivablesModel, InterventionKind, Outcome,
    ReceivablesDecisionModel, ReceivablesDecisionTrail, decide_receivable, features_for)
from .receivables_lab import (
    ArmReport, LatentAccount, LearnBoundaryError, LearnedModel, NaiveModel, assert_strategy_only,
    evaluate_arm, make_corpus, net_value, optimal_action, run_experiment)
from .receivables_benchmark import BENCHMARK_VERSION, benchmark_report, run_ladder
from .transitions import (
    AccountState, Admissibility, TransitionResult, VerifiedExperience, apply_transition,
    experience_from_transition)

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
    # governed run → canonical evidence + receipts (Phase B)
    "canonical_evidence", "receipts_for_run", "govern_run", "GovernedMissionResult", "GovernedStep",
    # cross-app Receivables Mission (Phase C)
    "investigate_receivables", "propose_followups", "ReceivableCandidate", "FollowupProposal",
    # decision-learning instrumentation (Phase D)
    "DecisionPoint", "InterventionKind", "AccountFeatures", "DecisionContext", "Candidate",
    "CandidateSet", "DecisionProposal", "DecisionRecord", "Outcome", "DecisionExperience",
    "ReceivablesDecisionModel", "DeterministicReceivablesModel", "ReceivablesDecisionTrail",
    "features_for", "decide_receivable",
    # decision-learning experiment / ablation (Phase E, synthetic + honestly labeled)
    "run_experiment", "evaluate_arm", "make_corpus", "optimal_action", "net_value", "LatentAccount",
    "ArmReport", "NaiveModel", "LearnedModel", "assert_strategy_only", "LearnBoundaryError",
    # full A→H ladder + frozen benchmark artifact (Phase F/G)
    "run_ladder", "benchmark_report", "BENCHMARK_VERSION",
    # verified-transition kernel (self-learning doc §2)
    "AccountState", "Admissibility", "TransitionResult", "apply_transition", "VerifiedExperience",
    "experience_from_transition",
]
