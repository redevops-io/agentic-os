"""Core data model for Mission Runtime.

A Mission is a *program*. It does NOT own a graph: `Mission -> ExecutionPlan (revisions)
-> ExecutionGraph`. A mission executes over an event-sourced *world state* blackboard, not
by re-querying apps. Observations become *beliefs* (confidence-weighted) before they touch
world state. Capabilities are *syscalls* — typed, metered, permissioned units with rich
metadata the planner and simulator reason over.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

#: Semantic version of the canonical cross-runtime plan contract — ``ExecutionPlan`` together with
#: ``PlanAxes`` (the six logical planning dimensions), ``ExecutionGraph`` and ``GovernancePlan``. This is
#: the object every runtime (Python spec, Go and Kotlin ports) speaks; Discovery and the planner build it
#: through adapters. Compatibility within a major is additive-only (new optional fields, new axes, new
#: node/edge kinds); a breaking change to the plan shape, its serialization, or its hashing bumps the major.
#: The intelligence that *generates* the best plan is the product; this typed object is the public contract.
EXECUTION_PLAN_CONTRACT_VERSION = "execution-plan/v8"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


# ─── enums ───────────────────────────────────────────────────────────────────
class MissionState(str, Enum):
    PLANNING = "planning"
    SIMULATING = "simulating"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    PAUSED = "paused"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"


class NodeState(str, Enum):
    PENDING = "pending"      # deps not yet satisfied
    READY = "ready"          # deps satisfied, awaiting dispatch
    RUNNING = "running"
    WAITING = "waiting"      # human task parked
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    COMPENSATED = "compensated"


# ─── budget / cost ───────────────────────────────────────────────────────────
@dataclass
class Budget:
    usd: float = 5.0
    tokens: int = 2_000_000
    human_minutes: float = 60.0
    wall_clock_s: float = 86_400.0


@dataclass
class NodeCost:
    usd: float = 0.0
    latency_ms: int = 0
    tokens: int = 0
    human_minutes: float = 0.0


# ─── capabilities (syscalls) ─────────────────────────────────────────────────
@dataclass
class CapabilitySpec:
    """One capability an operator (app) exposes. Metadata is what makes the planner smart."""
    name: str                                   # e.g. "crm.send_outreach"
    operator: str                               # owning app/module
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    cost: NodeCost = field(default_factory=NodeCost)
    permissions: list[str] = field(default_factory=list)
    deterministic: bool = True                  # same input -> same output?
    side_effecting: bool = False                # touches the outside world?
    undo: str | None = None                     # compensating capability (sagas) or None
    approval_required: bool = False
    confidence: float = 0.9                     # learned success rate (Capability Learning)
    estimated_value: str = "medium"             # low|medium|high
    provides: list[str] = field(default_factory=list)  # world-state keys/outcomes it can satisfy
    embedding: list[float] = field(default_factory=list)  # for semantic discovery
    source: str = ""                            # provenance/origin (e.g. "plugin:<name>"); "" = built-in/trusted
    # ── security surface (v0.3.x, additive) — the metadata the opt-in Executor security seams read
    # (isolation_for / authority_for) and the SecurityMonitor projects into boundary telemetry. All empty
    # by default, so a capability that declares nothing is unchanged. Mirrors CapabilityDescriptor's fields.
    required_authority: list[str] = field(default_factory=list)   # permissions the caller's authority must cover
    isolation_class: str = ""                   # "" | "in_process" | "sandbox" | "strict" — executor confinement
    network: list[str] = field(default_factory=list)              # declared egress endpoints (finer than permissions)
    data_classifications: list[str] = field(default_factory=list)  # sensitivity of the data it touches (e.g. "pii")
    secrets: list[str] = field(default_factory=list)             # named credential refs the broker issues JIT (never raw values)
    # ── concurrency surface (v0.3.x, additive) — canonical safety semantics the scheduler reads to decide
    # which ready nodes may run together. Describes SAFETY, not mechanism (no thread/async concepts leak in).
    # A capability that declares nothing keeps today's behaviour: no resource lock, bounded only by the
    # global concurrency cap. See mission/concurrency.py + scheduler Phase C.
    concurrency_mode: str = ""       # "" | "read_only" | "idempotent" | "side_effecting" | "exclusive"
    concurrency_key: str = ""        # resource-key template resolved from inputs, e.g. "crm:account:{account_id}"
    resource_keys: list[str] = field(default_factory=list)   # static conflict keys held while running (e.g. "k8s:cluster:prod")
    max_parallelism: int | None = None   # max concurrent holders of this cap's key(s); None -> mode default (exclusive=1)

    def key(self) -> str:
        return self.name


@dataclass
class CapabilityManifest:
    """What one app publishes at GET /capabilities."""
    operator: str
    capabilities: list[CapabilitySpec] = field(default_factory=list)


# ─── planner output: logical plan ────────────────────────────────────────────
@dataclass
class IntentStep:
    """A LOGICAL step — an outcome that must be achieved, NOT an app/tool/node."""
    outcome: str                                # world-state key/outcome to produce (e.g. "subscription")
    need: str                                   # natural-language capability need (for discovery)
    inputs_from: list[str] = field(default_factory=list)  # outcomes of upstream steps
    constraints: list[str] = field(default_factory=list)
    value_hint: str = "medium"
    cosmetic_inputs: list[str] = field(default_factory=list)  # inputs whose disagreement is NOT material (don't gate)
    id: str = field(default_factory=lambda: new_id("istep"))


@dataclass
class ExecutionIntent:
    """Mission Planner output — capability-agnostic. The Execution Compiler binds it to nodes."""
    mission_id: str
    steps: list[IntentStep] = field(default_factory=list)
    rationale: str = ""
    verified_fields: dict[str, str] = field(default_factory=dict)
    """The sealed intent's fields, when this plan came from one.

    A channel of their own rather than `IntentStep.constraints`, because the
    compiler substring-matches constraints for "human"/"review" to decide
    whether a node needs approval — so a *value* containing either would add an
    unrequested gate. Data must not be able to steer an authority decision."""
    template: str | None = None
    """Which template produced these steps, once the planner has decided.

    The caller's `template=` argument is a request; this is the answer. They
    differ whenever the planner chose one itself, and only the answer is
    reproducible — recovering the request means re-running whatever made the
    choice, which for `TemplatePlanner` means re-reading the goal prose."""
    id: str = field(default_factory=lambda: new_id("intent"))


# ─── compiler output: physical plan ──────────────────────────────────────────
@dataclass
class Node:
    """One capability invocation in the graph (physical)."""
    capability: str
    operator: str
    inputs: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    produces: str | None = None                 # world-state key this node writes
    approval_required: bool = False
    side_effecting: bool = False
    undo: str | None = None
    status: NodeState = NodeState.PENDING
    attempts: int = 0
    idempotency_key: str = ""
    cosmetic_inputs: list[str] = field(default_factory=list)  # $from_world inputs whose disagreement is non-material
    result: dict[str, Any] | None = None
    cost: NodeCost = field(default_factory=NodeCost)
    # ── concurrency surface (carried from the bound capability by the compiler) — the scheduler resolves
    # these into the conflict keys this node holds while running (see mission/concurrency.py).
    concurrency_mode: str = ""
    concurrency_key: str = ""
    resource_keys: list[str] = field(default_factory=list)
    max_parallelism: int | None = None
    assertions: list["StateAssertion"] = field(default_factory=list)  # P7: state-transition checks
    id: str = field(default_factory=lambda: new_id("node"))
    intent_step_id: str | None = None


@dataclass
class ExecutionGraph:
    """The physical plan — a capability DAG. Mirrors context_runtime/types.py:ExecutionGraph
    (kept local so the kernel is runnable standalone; converge the two later)."""
    nodes: list[Node] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("graph"))

    def by_id(self, nid: str) -> Node | None:
        return next((n for n in self.nodes if n.id == nid), None)


@dataclass
class SimResult:
    """Simulator projection for a plan (dry-run before execute)."""
    expected_usd: float = 0.0
    expected_success: float = 1.0
    expected_latency_ms: int = 0
    expected_approvals: int = 0
    within_budget: bool = True
    notes: list[str] = field(default_factory=list)


@dataclass
class PlanAxes:
    """The LOGICAL execution plan — the fields of ONE plan the runtime optimizes as a whole
    (Whitepaper v8):

        Goal -> Execution Plan { representation, retrieval, reasoning, model, execution topology,
                                 verification } -> Mission

    Given a goal the runtime does not make a scattered set of independent choices, it produces one plan
    (the way a relational optimizer produces one query plan). The Execution Compiler lowers these logical
    axes into the physical ExecutionGraph; `ExecutionPlan` carries both — the axes it was planned from and
    the graph it lowered to — plus the governing policy, budget and evidence requirements."""
    representation: str = ""          # documents | sql | property_graph | temporal | vision | code | ...
    retrieval: str = ""               # bm25 | dense | hybrid | graph | diver | ...
    reasoning: str = ""               # terse | reason | decompose | mapreduce (+sc/+v/effort)
    model: str = ""                   # model / tier chosen
    topology: str = "single_agent"    # one of mission.topology.TOPOLOGY_KINDS
    verification: str = ""            # citations | policy | fact | state_transition | none
    policy_refs: list[str] = field(default_factory=list)
    budget_usd: float = 0.0
    evidence_requirements: list[str] = field(default_factory=list)


@dataclass
class GovernancePlan:
    """The governance envelope for a plan — the single home for approvals, policy, verification and
    budget limits (nested under ExecutionPlan). Policy prunes plans BEFORE learning or model calls; every
    side-effecting node is gated unless autonomy says otherwise."""
    approvals_required: list[str] = field(default_factory=list)   # capabilities / nodes gated on a human
    policy_refs: list[str] = field(default_factory=list)          # permission grants the plan runs under
    verification: str = ""                                        # citations | policy | state_transition | none
    budget_usd: float = 0.0
    autonomy: str = "gated"                                       # gated | shadow | auto


@dataclass
class ExecutionPlan:
    """The ONE public cross-runtime schema (Whitepaper v8 · the "one execution plan").

        DiscoveryProposal → ExecutionPlan → CompiledMissionGraph (`graph`) → MissionRun → Outcome

    A single object spans the whole flow: the LOGICAL plan (`axes`, the six whitepaper dimensions), the
    governance envelope (`governance`), the planner's expectations, the `discovery_context` that triggered
    it, and the PHYSICAL plan it lowered to (`graph`, None until compiled). Every runtime (Python spec, Go
    and Kotlin ports) speaks this object; the intelligence that *generates* the best one is the product.
    Versioned as ``EXECUTION_PLAN_CONTRACT_VERSION`` (execution-plan/v8 — the Whitepaper v8 "one execution plan"). A mission has many revisions."""
    # cross-runtime identity + provenance (all optional so a plan can exist pre-mission, pre-compile)
    goal: str = ""
    mission_id: str = ""
    intent_id: str = ""
    discovery_context: dict[str, Any] = field(default_factory=dict)   # the proposal that triggered the plan
    axes: PlanAxes | None = None                 # the six whitepaper axes (representation/retrieval/…)
    governance: GovernancePlan | None = None     # governance envelope + planner expectations
    deadline: float | None = None
    expected_cost: float = 0.0
    expected_latency_ms: int = 0
    expected_quality: float = 0.0                # the planner's confidence in this plan (0..1)
    fallback: "ExecutionPlan | None" = None      # the next-best plan if this one misses the bar
    graph: ExecutionGraph | None = None          # the CompiledMissionGraph — None until the compiler runs
    revision: int = 1
    reason: str = "initial"                     # "initial" | "replan: <cause>"
    projection: SimResult | None = None
    id: str = field(default_factory=lambda: new_id("plan"))
    created_at: float = field(default_factory=now)

    # ── doc-named accessors: the sub-plans are nested fields of the one object ──────────────
    @property
    def plan_id(self) -> str:
        return self.id

    @property
    def representation_plan(self) -> str:
        return self.axes.representation if self.axes else ""

    @property
    def retrieval_plan(self) -> str:
        return self.axes.retrieval if self.axes else ""

    @property
    def reasoning_plan(self) -> str:
        return self.axes.reasoning if self.axes else ""

    @property
    def model_plan(self) -> str:
        return self.axes.model if self.axes else ""

    @property
    def topology_plan(self) -> str:
        return self.axes.topology if self.axes else "single_agent"

    @property
    def verification_plan(self) -> str:
        if self.governance and self.governance.verification:
            return self.governance.verification
        return self.axes.verification if self.axes else ""

    @property
    def evidence_requirements(self) -> list[str]:
        return list(self.axes.evidence_requirements) if self.axes else []

    @classmethod
    def from_axes(cls, axes: "PlanAxes", *, goal: str = "", discovery_context: dict | None = None,
                  governance: "GovernancePlan | None" = None, expected_cost: float = 0.0,
                  expected_latency_ms: int = 0, expected_quality: float = 0.0,
                  fallback: "ExecutionPlan | None" = None, deadline: float | None = None) -> "ExecutionPlan":
        """Build the canonical logical plan from the six-axis `PlanAxes` (pre-compilation)."""
        return cls(goal=goal, axes=axes, discovery_context=dict(discovery_context or {}),
                   governance=governance, expected_cost=expected_cost or (axes.budget_usd if axes else 0.0),
                   expected_latency_ms=expected_latency_ms, expected_quality=expected_quality,
                   fallback=fallback, deadline=deadline)

    def explain(self) -> dict:
        """Unified EXPLAIN across discovery → planner → governance → compiled graph."""
        a = self.axes
        return {
            "plan_id": self.id,
            "goal": self.goal,
            "discovery_context": self.discovery_context,
            "logical_plan": {
                "representation": a.representation if a else "",
                "retrieval": a.retrieval if a else "",
                "reasoning": a.reasoning if a else "",
                "model": a.model if a else "",
                "topology": a.topology if a else "single_agent",
                "verification": self.verification_plan,
            },
            "governance": asdict(self.governance) if self.governance else None,
            "evidence_requirements": self.evidence_requirements,
            "expected": {"cost": self.expected_cost, "latency_ms": self.expected_latency_ms,
                         "quality": self.expected_quality},
            "fallback": (asdict(self.fallback.axes) if self.fallback and self.fallback.axes else None),
            "compiled": ({"graph_id": self.graph.id, "nodes": len(self.graph.nodes)}
                         if self.graph else None),
            "revision": self.revision,
            "reason": self.reason,
        }


# ─── humans as nodes ─────────────────────────────────────────────────────────
@dataclass
class HumanTask:
    mission_id: str
    node_id: str
    assignee: str                               # role or user (resolved via permissions plane)
    prompt: str
    evidence: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=lambda: ["approve", "reject"])
    decision: str | None = None
    deadline: float | None = None
    id: str = field(default_factory=lambda: new_id("htask"))


# ─── world state + belief ────────────────────────────────────────────────────
@dataclass
class DecisionEvidence:
    """One reader's evidence for a material interpreted field. Each independent reader (a regex,
    a model, a policy, a retrieval, a prior belief) contributes evidence for the same field. No
    ``source_type`` is privileged over another — the runtime never encodes 'regex wins' or
    'model wins'; when readers disagree on a material field the disagreement is routed to the
    controller, not silently resolved."""
    field: str
    value: Any
    source_type: str = "prior"          # regex | model | policy | retrieval | prior
    source_ref: str = ""                # rule id, span, chunk id, policy id
    confidence: float = 1.0
    id: str = field(default_factory=lambda: new_id("de"))


@dataclass
class Belief:
    """A confidence-weighted view of one fact, fused from possibly-disagreeing observations.
    ``evidence`` carries the per-reader ``DecisionEvidence`` the fusion saw (survives replay, as
    beliefs are recomputed from the observation event log)."""
    value: Any
    confidence: float = 1.0
    sources: list[str] = field(default_factory=list)
    conflict: bool = False
    updated_at: float = field(default_factory=now)
    evidence: list[DecisionEvidence] = field(default_factory=list)


@dataclass
class WorldFact:
    key: str
    belief: Belief


# ─── mission ─────────────────────────────────────────────────────────────────
@dataclass
class Mission:
    """The program. Holds a world-state ref and the ACTIVE plan id — never a graph directly."""
    goal: str
    constraints: list[str] = field(default_factory=list)
    policy_refs: list[str] = field(default_factory=list)   # permission grants this mission runs under
    policy: "MissionPolicy | None" = None                  # named, versioned policy — the single authority
    budget: Budget = field(default_factory=Budget)
    deadline: float | None = None
    state: MissionState = MissionState.PLANNING
    world_state_id: str = ""
    active_plan_id: str | None = None
    outcome: dict[str, Any] | None = None
    template: str | None = None
    # Evidence identity carried across the Discovery→Mission boundary (v0.2.x Slice 1): the sealed
    # VerifiedIntent this mission was created from, and the exact evidence refs it consumed — so the
    # seal is no longer dropped at the first hop and replay/EXPLAIN can resolve what was actually used.
    intent_content_hash: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    context_epoch_id: str = ""              # the ContextEpoch (ContextView) this mission's plan is pinned to
    id: str = field(default_factory=lambda: new_id("mission"))
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)


# ─── learning ────────────────────────────────────────────────────────────────
@dataclass
class MissionOutcomeEvent:
    mission_id: str
    kind: str                                   # "invoice_paid" | "ticket_resolved" | ...
    success: bool
    business_value: float = 0.0
    cost: NodeCost = field(default_factory=NodeCost)
    plan_signature: str = ""                    # capability-sequence shape (workflow-ordering learning)
    node_id: str | None = None


@dataclass
class Lesson:
    """Structured output of a Reflection pass — high-signal training data for planning."""
    mission_id: str
    failed_assumption: str = ""
    missing_verification: str = ""
    better_capability: str = ""
    evidence: list[str] = field(default_factory=list)
    success: bool = True


# ─── P7: evidence & verification plane (observed → verified → believed → committed) ──
class VerificationDecision(str, Enum):
    ACCEPT = "accept"      # the result may become authoritative world state
    ESCALATE = "escalate"  # unmet assertion / unbacked claim → a human decides
    REJECT = "reject"      # hard failure (malformed / permission) → cannot be accepted


@dataclass
class Claim:
    """A statement a verification must substantiate before a result is accepted."""
    statement: str
    subject: str = ""
    required_by_policy: str = ""
    id: str = field(default_factory=lambda: new_id("claim"))


@dataclass
class EvidenceRecord:
    """Append-only evidence backing a claim — the connective tissue of verification/EXPLAIN/learning."""
    claim_id: str
    source: str
    content: Any = None
    produced_at: float = field(default_factory=now)
    freshness_s: float | None = None
    id: str = field(default_factory=lambda: new_id("ev"))


@dataclass
class StateAssertion:
    """A checkable expectation about a result / world-state key (state-transition validation)."""
    key: str
    op: str = "present"                 # present | truthy | equals | in
    expected: Any = None
    observed: Any = None
    holds: bool | None = None


@dataclass
class VerificationResult:
    decision: VerificationDecision
    confidence: float = 1.0
    checks: list[dict] = field(default_factory=list)      # {stage, passed, detail}
    evidence_ids: list[str] = field(default_factory=list)
    verifier: str = "composite"
    id: str = field(default_factory=lambda: new_id("vr"))


# ─── P8: policy & human-decision plane (approval is a decision, not a static flag) ──
class ApprovalRule(str, Enum):
    """Which rule made approval necessary — an approval can fire for more than one."""
    MANDATORY = "mandatory_capability"     # capability.approval_required (legal/contractual/org)
    MISSION_POLICY = "mission_policy"      # the mission's own policy demanded it
    DYNAMIC_RISK = "dynamic_risk"          # the weighed risk×value assessment crossed threshold
    VERIFICATION = "verification_escalation"  # verification could not accept the result


@dataclass
class RiskFactors:
    """The dimensions a single risk×value threshold is too reductive to capture (P8)."""
    reversibility: float = 1.0             # 1 = fully reversible (undo present), 0 = irreversible
    uncertainty: float = 0.0               # 1 - min confidence across inputs / capability
    monetary: float = 0.0                  # action $ magnitude normalized against mission budget
    novelty: float = 0.0                   # 1 - learned track record for the capability
    blast_radius: float = 0.0              # normalized count of downstream dependents
    verification_coverage: float = 1.0     # 1 = a consequential result was verified this run
    permissioned: bool = True              # capability permissions ⊆ mission grants
    regulatory: bool = False               # regulatory / compliance-sensitive capability
    score: float = 0.0                     # composite risk in [0, 1]


@dataclass
class ApprovalDecision:
    """The output of the policy plane: whether a human gate is required and *why*, with an
    evidence-backed approval packet for the approver."""
    required: bool
    rules: list[str] = field(default_factory=list)        # ApprovalRule values that fired
    risk: RiskFactors = field(default_factory=RiskFactors)
    packet: dict = field(default_factory=dict)            # action · evidence · impact · alternatives
    id: str = field(default_factory=lambda: new_id("apd"))
    # mission-policy/v1 — the evaluated policy pinned onto the decision (empty when no MissionPolicy)
    denied: bool = False                                  # a policy DENY rule hard-blocked the node
    policy_ref: str = ""                                  # "finance-prod@12"
    policy_digest: str = ""                               # "sha256:…" — replayable policy identity
    explain: dict = field(default_factory=dict)           # {matched_rule, reason, suggested_action, trace}


def to_jsonable(obj: Any) -> Any:
    """Best-effort JSON view of the dataclasses above (for events + API)."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj
