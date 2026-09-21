"""External Agent Gateway (agent-gateway/v1) — personal agents as inbound Mission initiators and
governed outbound execution nodes, plus a provider capability audit.

This subpackage EXTENDS the governed gateway (``agentic_os.agent_gateway``) rather than replacing it.
It reuses the existing pipeline, capability registry, approval inbox, egress engine and OAuth/MCP
surface; it adds only what those did not have:

  * a provider-neutral :class:`ExternalAgentAdapter` with a background-task lifecycle
    (submit / status / cancel / provide-input) for agents like Muse/ChatGPT/Claude;
  * an :class:`ExternalAgentOperator` that runs an external agent as a governed execution node
    through the REAL ``agentic_os.mission.operator_sdk`` + ``projects`` Decision/ActionReceipt path;
  * an approval bridge that binds a trusted approval to a ``Decision.decision_id`` + the exact
    action digest (the gateway already bound the digest; this adds the Decision link);
  * normalized Evidence + verification that never auto-trusts a provider success claim;
  * Edge Sentinel observation via the REAL ``agentic_os.mission.events.RuntimeEvent`` (v10) envelope.

Non-negotiable invariants (plan §3): external-agent text is never authorization; approval is
identity- and version-bound; external agents cannot bypass Governance; ReDevOps owns durable context;
provider results are evidence, not truth; Mission correctness never depends on provider availability;
Learn may improve strategy but never modifies authorization or deterministic gates.
"""
from .contracts import (
    EXTERNAL_CONTRACT_VERSION, AgentCapabilities, AgentIdentity, AgentPermissionScope,
    AgentTaskInput, AgentTaskRef, AgentTaskRequest, AgentTaskResult, AgentTaskStatus,
    CapabilityStatus, ExternalAgentAdapter, TaskState, TERMINAL_STATES, load_capability_audit)
from .fake_adapter import FakeExternalAgentAdapter
from .lifecycle import ManagedTask, TaskManager, TaskManagerError
from .verification import VerificationOutcome, VerificationStatus, verify_result
from .operator import ExternalAgentOperator
from .approval_bridge import ApprovalError, ExternalApproval, ExternalApprovalBridge
from .inbound import InboundError, InboundExternalAgentBridge, MissionHandle
from .observation import ExternalAgentObserver
from .evaluation import (
    ADVERSARIAL_CORPUS, LEARN_FORBIDDEN_FIELDS, LEARN_STRATEGY_FIELDS, LearnBoundaryError,
    assert_strategy_only, run_adversarial_corpus)
from .projections import external_action_view, social_mission_view
from .deployment_inspection import (
    DeploymentFinding, InspectionReport, build_remediation_request, demo_inspection_mission,
    derive_findings, run_inspection)

__all__ = [
    "EXTERNAL_CONTRACT_VERSION",
    # contracts (Phase 1)
    "AgentIdentity", "AgentCapabilities", "CapabilityStatus", "AgentPermissionScope",
    "AgentTaskRequest", "AgentTaskRef", "AgentTaskStatus", "AgentTaskInput", "AgentTaskResult",
    "TaskState", "TERMINAL_STATES", "ExternalAgentAdapter", "load_capability_audit",
    # fake adapter (Phase 8 fixture)
    "FakeExternalAgentAdapter",
    # lifecycle (Phase 5)
    "TaskManager", "ManagedTask", "TaskManagerError",
    # verification (Phase 6)
    "verify_result", "VerificationOutcome", "VerificationStatus",
    # governed operator (Phase 4)
    "ExternalAgentOperator",
    # approval bridge (Phase 3)
    "ExternalApprovalBridge", "ExternalApproval", "ApprovalError",
    # inbound mission delegation (Phase 2)
    "InboundExternalAgentBridge", "MissionHandle", "InboundError",
    # Edge Sentinel observation (Phase 7)
    "ExternalAgentObserver",
    # adversarial eval + Learn boundary (Phase 10)
    "run_adversarial_corpus", "ADVERSARIAL_CORPUS", "assert_strategy_only", "LearnBoundaryError",
    "LEARN_STRATEGY_FIELDS", "LEARN_FORBIDDEN_FIELDS",
    # Projects/Sidekick projections + live deployment-inspection mission (Phase 9)
    "external_action_view", "social_mission_view", "run_inspection", "demo_inspection_mission",
    "derive_findings", "build_remediation_request", "DeploymentFinding", "InspectionReport",
]
