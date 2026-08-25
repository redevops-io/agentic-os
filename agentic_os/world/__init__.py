"""World-runtime — orchestrate dataset worlds end-to-end through the governed loop for demo + product.

Built on the ``runtime_contracts.world`` contract (WorldEvent / IdentityGraph / WorldRegistry / VisualTrace).
Provides the Capability Fabric (plan against capabilities, substitute providers), the Outcome Simulator
(safe write target), the Projection Seeder (one world → many app cores, one identity), the Scenario
Orchestrator (run / replay / inject failures / control the clock), and the Benchmark Runner (compare the
Runtime against manual / naive-agent / independent-agent baselines). Worlds are definitions, not new code.
"""
from __future__ import annotations

from .base import RuntimeContext, WorldDefinition
from .baseline import BenchmarkRunner
from .fabric import CapabilityDenied, CapabilityFabric, CapabilityProvider, Invocation, SideEffect
from .flagship import AfterHoursLeadWorld
from .gtm import FindCompaniesRebuildingTheRuntime
from .sponsorship import CreatorAcquisitionWorld
from .sponsorship_economics import (
    GovernedSponsorshipWorld, SponsorshipQuote, NormalizedEconomics, normalize_quote,
    PlacementProposal, SponsorshipApproval, BookingDecision, evaluate_booking,
    Claim, CreativeBrief, build_creative_brief, CreatorAttribution, build_attribution)
from .outreach import (OutreachContext, OutreachDecision, decide, render_email, send_outreach,
                       quality_gate, select_template, SuppressionLedger, handle_unsubscribe,
                       unsubscribe_url, unsubscribe_token)
from .enrichment import (get_provider, resolve_verified_email, resolve_contact, title_keywords,
                         source_apollo_list, ApolloProvider, HunterProvider, ClearbitProvider)
from .optimizer import CandidateAction, CrossChannelOptimizer, Allocation, direct_email_action, sponsorship_action
from .attention import (AttentionItem, AttentionKind, Autonomy, build_attention_queue, items_from_run,
                        summarize)
from .models import (
    BaselineResult,
    ExecutionMode,
    Perturbation,
    PerturbationKind,
    RunMetrics,
    Scorecard,
    WorldRun,
)
from .orchestrator import ScenarioOrchestrator
from .seeder import ProjectionSeeder
from .simulator import OutcomeSimulator, SimArtifact
from .worlds import FinanceLeakageWorld, KycOnboardingWorld
from .worlds import ALL_WORLDS as _OTHER_WORLDS

#: every world the engine ships — the demo Dataset selector reads this (flagship first).
ALL_WORLDS = {AfterHoursLeadWorld().world_id: AfterHoursLeadWorld(),
              FindCompaniesRebuildingTheRuntime().world_id: FindCompaniesRebuildingTheRuntime(),
              CreatorAcquisitionWorld().world_id: CreatorAcquisitionWorld(),
              GovernedSponsorshipWorld().world_id: GovernedSponsorshipWorld(),
              **_OTHER_WORLDS}

__all__ = [
    "WorldDefinition", "RuntimeContext",
    "CapabilityFabric", "CapabilityProvider", "Invocation", "SideEffect", "CapabilityDenied",
    "OutcomeSimulator", "SimArtifact", "ProjectionSeeder",
    "ScenarioOrchestrator", "BenchmarkRunner",
    "ExecutionMode", "Perturbation", "PerturbationKind", "RunMetrics", "WorldRun", "BaselineResult", "Scorecard",
    "AfterHoursLeadWorld", "FindCompaniesRebuildingTheRuntime", "CreatorAcquisitionWorld",
    "GovernedSponsorshipWorld", "SponsorshipQuote", "NormalizedEconomics", "normalize_quote",
    "PlacementProposal", "SponsorshipApproval", "BookingDecision", "evaluate_booking",
    "Claim", "CreativeBrief", "build_creative_brief", "CreatorAttribution", "build_attribution",
    "KycOnboardingWorld",
    "FinanceLeakageWorld", "ALL_WORLDS",
    "OutreachContext", "OutreachDecision", "decide", "render_email", "send_outreach",
    "quality_gate", "select_template", "SuppressionLedger", "handle_unsubscribe",
    "unsubscribe_url", "unsubscribe_token",
    "get_provider", "resolve_verified_email", "resolve_contact", "title_keywords", "source_apollo_list",
    "ApolloProvider", "HunterProvider", "ClearbitProvider",
    "CandidateAction", "CrossChannelOptimizer", "Allocation", "direct_email_action", "sponsorship_action",
    "AttentionItem", "AttentionKind", "Autonomy", "build_attention_queue", "items_from_run", "summarize",
]
