"""Mission Runtime — the kernel of the Agentic OS.

CANONICAL KERNEL (reconciliation 2026-10-05): this public module is the single source of truth for the Mission
kernel. An enterprise copy (``agentic_os_enterprise.mission``) had forked and drifted — equal on most modules,
ahead here on the execution breadth (runtime/executor/merge/compiler), ahead there only on multi-tenancy. The
contract is unified by making THIS ``Node`` the superset (it carries both ``cosmetic_inputs`` and the Phase-3
``resource`` tenancy field), so both planes share one ``types``; the enterprise side re-exports this kernel and
keeps only its tenancy overlay (``tenant_runtime``). Change the contract here, not there.

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
