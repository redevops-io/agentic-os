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
from .decision_episode import (
    DecisionEpisode,
    CONTRACT_VERSION as DECISION_EPISODE_CONTRACT_VERSION,
)
from .decision_provider import (
    DecisionProvider,
    DecisionRequest,
    DecisionResult,
    StaticDecisionProvider,
    validate_decision,
    CONTRACT_VERSION as DECISION_PROVIDER_CONTRACT_VERSION,
)
from .assessment import (
    Assessment,
    ComparabilityStatus,
    ComparabilityVerdict,
    EvaluationMethodology,
    PublicationDecision,
    PublicationDecisionValue,
    PublicationGate,
    PublicationGateStatus,
    comparability_verdict,
    publication_decision_from_gates,
    CONTRACT_VERSION as ASSESSMENT_PUBLICATION_CONTRACT_VERSION,
)
from .policy_promotion import (
    PolicyCandidate,
    PolicyCandidateState,
    PolicyPromotionDecision,
    PolicyPromotionEvidence,
    PolicyPromotionGate,
    PolicyPromotionStatus,
    decide_policy_promotion,
    CONTRACT_VERSION as POLICY_PROMOTION_CONTRACT_VERSION,
)
from .apps_e2e_runner import (
    AppExecutionOutcome,
    AppsE2EBenchmarkReport,
    EpisodeAppendReceipt,
    EpisodeEvidenceBundle,
    InMemoryEpisodeStore,
    JsonlEpisodeStore,
    ScenarioRun,
    evaluation_program_for_suite,
    run_scenario,
    scenario_run_from_payload,
    summarize_runs,
    CONTRACT_VERSION as APPS_E2E_RUNNER_CONTRACT_VERSION,
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
    # decision-provider/v1 and decision-episode/v1 — bounded model decisions and learning records
    "DecisionProvider", "DecisionRequest", "DecisionResult", "StaticDecisionProvider", "validate_decision",
    "DECISION_PROVIDER_CONTRACT_VERSION", "DecisionEpisode", "DECISION_EPISODE_CONTRACT_VERSION",
    # assessment-publication/v1 — RAAAL-style facts, comparability and surface publication gates
    "Assessment", "ComparabilityStatus", "ComparabilityVerdict", "EvaluationMethodology",
    "PublicationDecision", "PublicationDecisionValue", "PublicationGate", "PublicationGateStatus",
    "comparability_verdict", "publication_decision_from_gates", "ASSESSMENT_PUBLICATION_CONTRACT_VERSION",
    # policy-promotion/v1 — shadow learning promotion and rollback gates
    "PolicyCandidate", "PolicyCandidateState", "PolicyPromotionDecision", "PolicyPromotionEvidence",
    "PolicyPromotionGate", "PolicyPromotionStatus", "decide_policy_promotion",
    "POLICY_PROMOTION_CONTRACT_VERSION",
    # apps-e2e-runner/v1 — scenario/provider/oracle adapter onto decision episodes + Evaluation Runtime
    "AppExecutionOutcome", "AppsE2EBenchmarkReport", "EpisodeAppendReceipt", "EpisodeEvidenceBundle",
    "InMemoryEpisodeStore", "JsonlEpisodeStore", "ScenarioRun", "evaluation_program_for_suite",
    "run_scenario", "scenario_run_from_payload", "summarize_runs", "APPS_E2E_RUNNER_CONTRACT_VERSION",
]
