"""Mission Runtime — the kernel of the Agentic OS (Whitepaper v5).

Layered like a database engine (see docs/architecture.md):

    Mission Planner   (what should happen?)   -> ExecutionIntent   [LLM, logical]
    Execution Compiler(in what order/with what?)-> ExecutionPlan.graph [deterministic, physical]
    Scheduler         (when/where?)           -> dispatch waves
    Mission Runtime   (what's running now?)   -> state machine
    Executor          (run a node durably)    -> node results       [Dagster in prod]
    Capability Runtime(run one capability)    -> Context Runtime
    Apps              (domain work)           -> operators

Everything here is pure-Python and dependency-light. The heavy external pieces —
Dagster (executor), a live thinking model (planner), real embeddings (discovery) —
sit behind injectable interfaces (Protocols) so production wiring is a swap, not a
rewrite. The in-repo defaults make the whole kernel runnable and testable in-process.
"""
from .types import (
    Mission, MissionState, ExecutionIntent, IntentStep, ExecutionPlan, PlanAxes, GovernancePlan,
    Node, NodeState, NodeCost, Budget, HumanTask, CapabilityManifest, CapabilitySpec, WorldFact,
    Belief, DecisionEvidence, MissionOutcomeEvent, SimResult, Lesson, ExecutionGraph,
    EXECUTION_PLAN_CONTRACT_VERSION,
)
from .runtime import MissionRuntime
from .policy import (
    MissionPolicy, PolicyRule, Effect, NodeContext, PolicyOutcome, from_constraints,
    CONTRACT_VERSION as MISSION_POLICY_CONTRACT_VERSION,
)

__all__ = [
    "Mission", "MissionState", "ExecutionIntent", "IntentStep", "ExecutionPlan", "PlanAxes",
    "GovernancePlan", "EXECUTION_PLAN_CONTRACT_VERSION", "Node",
    "NodeState", "NodeCost", "Budget", "HumanTask", "CapabilityManifest", "CapabilitySpec",
    "WorldFact", "Belief", "DecisionEvidence", "MissionOutcomeEvent", "SimResult", "Lesson", "ExecutionGraph",
    "MissionRuntime",
    # mission-policy/v1 — policy as a first-class, versioned, digest-pinned object
    "MissionPolicy", "PolicyRule", "Effect", "NodeContext", "PolicyOutcome", "from_constraints",
    "MISSION_POLICY_CONTRACT_VERSION",
]
