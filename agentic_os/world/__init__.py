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
ALL_WORLDS = {AfterHoursLeadWorld().world_id: AfterHoursLeadWorld(), **_OTHER_WORLDS}

__all__ = [
    "WorldDefinition", "RuntimeContext",
    "CapabilityFabric", "CapabilityProvider", "Invocation", "SideEffect", "CapabilityDenied",
    "OutcomeSimulator", "SimArtifact", "ProjectionSeeder",
    "ScenarioOrchestrator", "BenchmarkRunner",
    "ExecutionMode", "Perturbation", "PerturbationKind", "RunMetrics", "WorldRun", "BaselineResult", "Scorecard",
    "AfterHoursLeadWorld", "KycOnboardingWorld", "FinanceLeakageWorld", "ALL_WORLDS",
]
