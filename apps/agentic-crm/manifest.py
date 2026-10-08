"""agentic-crm AppManifest — the runtime-native registration contract (plan §2.1/§3.2).

Declares the app's decision producer (crm_nba, via ``producers``), the capabilities it provides
(the four the operator registers), a verifier per side-effecting capability (the Mission Runtime's
composite verification reads each capability's declared output back), the outcome kinds it emits,
the core it wraps and at what integration level, and its data/privacy profile. ``register`` binds it
to the live operator through the ``AppRegistry``, which fails closed on any conformance violation.
"""
from __future__ import annotations

from agentic_os.app_kit import DeploySpec

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.app_kit import (
    AppManifest,
    CapabilityRef,
    CoreRequirement,
    IntegrationLevel,
    OutcomeKind,
    PrivacyProfile,
    ProducerRef,
    VerifierRef,
)
from agentic_os.governance.classification import DataClassification
from agentic_os.governance.routing import ExecutionBoundary

from . import producers

MANIFEST = AppManifest(
    name="agentic-crm",
    version="0.1.0",
    producers=(
        ProducerRef("crm_nba", opportunity_kinds=("next_best_action",),
                    emits_capabilities=producers.EMITTED_CAPABILITIES),
    ),
    capabilities=(
        CapabilityRef("crm.score_lead", risk_tier=RiskTier.BOUNDED_WRITE),
        CapabilityRef("crm.research", risk_tier=RiskTier.BOUNDED_WRITE),
        CapabilityRef("crm.draft_outreach", risk_tier=RiskTier.BOUNDED_WRITE),
        CapabilityRef("crm.qualify", risk_tier=RiskTier.BOUNDED_WRITE),
    ),
    # Every CRM action writes to ERPNext (side_effecting) → the runtime's composite verifier reads the
    # declared output back before the node commits (N4).
    verifiers=(
        VerifierRef("crm.score_lead", "composite:lead_score"),
        VerifierRef("crm.research", "composite:lead_research"),
        VerifierRef("crm.draft_outreach", "composite:outreach_draft"),
        VerifierRef("crm.qualify", "composite:lead_qualified"),
    ),
    outcome_kinds=(
        OutcomeKind("send_proposal", unit="reply|meeting|conversion", expected_delay_s=86400.0),
        OutcomeKind("nurture_email", unit="reply|conversion", expected_delay_s=172800.0),
        OutcomeKind("answer_question", unit="reply", expected_delay_s=3600.0),
    ),
    required_cores=(CoreRequirement("erpnext", IntegrationLevel.L1_EXTENSION),),
    privacy=PrivacyProfile(
        data_classes=(DataClassification.CUSTOMER_CONFIDENTIAL,),
        execution_boundary=ExecutionBoundary.IN_BOUNDARY,
    ),
    data_classifications=(DataClassification.CUSTOMER_CONFIDENTIAL,),
    deploy=DeploySpec(
        port=8210,
        pain='sales pipeline & CRM',
        tagline='A pipeline that scores, researches, and drafts outreach on a real CRM.',
        agents=('score', 'research', 'draft', 'qualify'),
        approval=('send',),
        deploy='compose',
    ),
)


def register(registry, operator=None):
    """Register agentic-crm with an ``AppRegistry`` (fails closed if non-conformant). Builds the live
    operator via ``build_crm_operator`` when one is not supplied."""
    if operator is None:
        from . import operator as operator_module
        operator = operator_module.build_crm_operator()
    return registry.register(MANIFEST, operator)


__all__ = ["MANIFEST", "register"]
