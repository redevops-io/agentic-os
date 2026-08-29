// Package mission is the Go port of the Python Mission Runtime kernel
// (agentic_os/mission). A Mission is a program: Mission → ExecutionPlan (revisions) →
// ExecutionGraph, executed over an event-sourced world-state blackboard. Observations
// become confidence-weighted beliefs before they touch world state. Capabilities are
// syscalls — typed, metered, permissioned units with rich planner/simulator metadata.
package mission

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"time"
)

// newID returns "prefix_<12 hex>", mirroring Python new_id.
func newID(prefix string) string {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return prefix + "_" + hex.EncodeToString(b)
}

// now returns wall-clock seconds as a float, mirroring Python time.time().
func now() float64 { return float64(time.Now().UnixNano()) / 1e9 }

// ─── enums ───────────────────────────────────────────────────────────────────

type MissionState string

const (
	MissionPlanning     MissionState = "planning"
	MissionSimulating   MissionState = "simulating"
	MissionRunning      MissionState = "running"
	MissionWaitingHuman MissionState = "waiting_human"
	MissionPaused       MissionState = "paused"
	MissionFailed       MissionState = "failed"
	MissionSucceeded    MissionState = "succeeded"
	MissionCancelled    MissionState = "cancelled"
)

type NodeState string

const (
	NodePending     NodeState = "pending"
	NodeReady       NodeState = "ready"
	NodeRunning     NodeState = "running"
	NodeWaiting     NodeState = "waiting"
	NodeDone        NodeState = "done"
	NodeFailed      NodeState = "failed"
	NodeSkipped     NodeState = "skipped"
	NodeCompensated NodeState = "compensated"
)

// ─── budget / cost ───────────────────────────────────────────────────────────

type Budget struct {
	USD          float64 `json:"usd"`
	Tokens       int     `json:"tokens"`
	HumanMinutes float64 `json:"human_minutes"`
	WallClockS   float64 `json:"wall_clock_s"`
}

// NewBudget mirrors the Python Budget defaults (usd=5, tokens=2M, 60m, 1 day).
func NewBudget() Budget {
	return Budget{USD: 5.0, Tokens: 2_000_000, HumanMinutes: 60.0, WallClockS: 86_400.0}
}

type NodeCost struct {
	USD          float64 `json:"usd"`
	LatencyMs    int     `json:"latency_ms"`
	Tokens       int     `json:"tokens"`
	HumanMinutes float64 `json:"human_minutes"`
}

// ─── capabilities (syscalls) ─────────────────────────────────────────────────

// CapabilitySpec is one capability an operator (app) exposes. Metadata is what makes the
// planner smart.
type CapabilitySpec struct {
	Name             string            `json:"name"`
	Operator         string            `json:"operator"`
	Inputs           map[string]string `json:"inputs"`
	Outputs          map[string]string `json:"outputs"`
	Cost             NodeCost          `json:"cost"`
	Permissions      []string          `json:"permissions"`
	Deterministic    bool              `json:"deterministic"`
	SideEffecting    bool              `json:"side_effecting"`
	Undo             string            `json:"undo"` // "" ⇒ no compensating capability
	ApprovalRequired bool              `json:"approval_required"`
	Confidence       float64           `json:"confidence"`
	EstimatedValue   string            `json:"estimated_value"` // low|medium|high
	Provides         []string          `json:"provides"`
	Embedding        []float64         `json:"embedding"`
	Source           string            `json:"source"` // provenance/origin ("plugin:<name>"); "" = built-in/trusted
	// Concurrency surface (v0.3.x, additive) — canonical safety semantics the scheduler reads to decide
	// which ready nodes may run together. Describes SAFETY, not mechanism. Empty ⇒ today's behaviour.
	ConcurrencyMode string   `json:"concurrency_mode"` // "" | "read_only" | "idempotent" | "side_effecting" | "exclusive"
	ConcurrencyKey  string   `json:"concurrency_key"`  // resource-key template resolved from inputs, e.g. "crm:account:{account_id}"
	ResourceKeys    []string `json:"resource_keys"`    // static conflict keys held while running
	MaxParallelism  int      `json:"max_parallelism"`  // max concurrent holders of this cap's key(s); 0 ⇒ mode default (exclusive=1)
}

// NewCapabilitySpec applies the Python defaults (deterministic, confidence 0.9, medium value).
func NewCapabilitySpec(name, operator string) CapabilitySpec {
	return CapabilitySpec{
		Name: name, Operator: operator, Deterministic: true,
		Confidence: 0.9, EstimatedValue: "medium",
	}
}

func (c CapabilitySpec) Key() string { return c.Name }

// CapabilityManifest is what one app publishes at GET /capabilities.
type CapabilityManifest struct {
	Operator     string           `json:"operator"`
	Capabilities []CapabilitySpec `json:"capabilities"`
}

// ─── planner output: logical plan ────────────────────────────────────────────

// IntentStep is a LOGICAL step — an outcome to achieve, NOT an app/tool/node.
type IntentStep struct {
	Outcome     string   `json:"outcome"`
	Need        string   `json:"need"`
	InputsFrom  []string `json:"inputs_from"`
	Constraints []string `json:"constraints"`
	ValueHint   string   `json:"value_hint"`
	ID          string   `json:"id"`
}

func NewIntentStep(outcome, need string) IntentStep {
	return IntentStep{Outcome: outcome, Need: need, ValueHint: "medium", ID: newID("istep")}
}

// ExecutionIntent is the Mission Planner output — capability-agnostic; the compiler binds it.
type ExecutionIntent struct {
	MissionID string       `json:"mission_id"`
	Steps     []IntentStep `json:"steps"`
	Rationale string       `json:"rationale"`
	ID        string       `json:"id"`
}

func NewExecutionIntent(missionID string, steps ...IntentStep) ExecutionIntent {
	return ExecutionIntent{MissionID: missionID, Steps: steps, ID: newID("intent")}
}

// ─── compiler output: physical plan ──────────────────────────────────────────

// Node is one capability invocation in the graph (physical).
type Node struct {
	Capability       string           `json:"capability"`
	Operator         string           `json:"operator"`
	Inputs           map[string]any   `json:"inputs"`
	DependsOn        []string         `json:"depends_on"`
	Produces         string           `json:"produces"` // world-state key this node writes ("" ⇒ none)
	ApprovalRequired bool             `json:"approval_required"`
	SideEffecting    bool             `json:"side_effecting"`
	Undo             string           `json:"undo"`
	Status           NodeState        `json:"status"`
	Attempts         int              `json:"attempts"`
	IdempotencyKey   string           `json:"idempotency_key"`
	Result           map[string]any   `json:"result"`
	Cost             NodeCost         `json:"cost"`
	// Concurrency surface (carried from the bound capability by the compiler) — resolved by the scheduler
	// into the conflict keys this node holds while running (see concurrency.go).
	ConcurrencyMode  string           `json:"concurrency_mode"`
	ConcurrencyKey   string           `json:"concurrency_key"`
	ResourceKeys     []string         `json:"resource_keys"`
	MaxParallelism   int              `json:"max_parallelism"`
	Assertions       []StateAssertion `json:"assertions"` // P7 state-transition checks
	ID               string           `json:"id"`
	IntentStepID     string           `json:"intent_step_id"`
}

func NewNode(capability, operator string) Node {
	return Node{Capability: capability, Operator: operator, Status: NodePending, ID: newID("node")}
}

// ExecutionGraph is the physical plan — a capability DAG.
type ExecutionGraph struct {
	Nodes []Node `json:"nodes"`
	ID    string `json:"id"`
}

func NewExecutionGraph() *ExecutionGraph { return &ExecutionGraph{ID: newID("graph")} }

// ByID returns the node with the given id, or nil.
func (g *ExecutionGraph) ByID(nid string) *Node {
	for i := range g.Nodes {
		if g.Nodes[i].ID == nid {
			return &g.Nodes[i]
		}
	}
	return nil
}

// SimResult is the simulator projection for a plan (dry-run before execute).
type SimResult struct {
	ExpectedUSD       float64  `json:"expected_usd"`
	ExpectedSuccess   float64  `json:"expected_success"`
	ExpectedLatencyMs int      `json:"expected_latency_ms"`
	ExpectedApprovals int      `json:"expected_approvals"`
	WithinBudget      bool     `json:"within_budget"`
	Notes             []string `json:"notes"`
}

// NewSimResult mirrors the Python defaults (success 1.0, within budget).
func NewSimResult() SimResult {
	return SimResult{ExpectedSuccess: 1.0, WithinBudget: true}
}

// ExecutionPlan is a compiled revision. A mission has MANY plans over its life.
type ExecutionPlan struct {
	MissionID  string          `json:"mission_id"`
	IntentID   string          `json:"intent_id"`
	Graph      *ExecutionGraph `json:"graph"`
	Revision   int             `json:"revision"`
	Reason     string          `json:"reason"`
	Projection *SimResult      `json:"projection"`
	ID         string          `json:"id"`
	CreatedAt  float64         `json:"created_at"`
}

func NewExecutionPlan(missionID, intentID string, graph *ExecutionGraph) *ExecutionPlan {
	return &ExecutionPlan{
		MissionID: missionID, IntentID: intentID, Graph: graph,
		Revision: 1, Reason: "initial", ID: newID("plan"), CreatedAt: now(),
	}
}

// ─── humans as nodes ─────────────────────────────────────────────────────────

type HumanTask struct {
	MissionID string   `json:"mission_id"`
	NodeID    string   `json:"node_id"`
	Assignee  string   `json:"assignee"`
	Prompt    string   `json:"prompt"`
	Evidence  []string `json:"evidence"`
	Options   []string `json:"options"`
	Decision  string   `json:"decision"`
	Deadline  *float64 `json:"deadline"`
	ID        string   `json:"id"`
}

func NewHumanTask(missionID, nodeID, assignee, prompt string) HumanTask {
	return HumanTask{
		MissionID: missionID, NodeID: nodeID, Assignee: assignee, Prompt: prompt,
		Options: []string{"approve", "reject"}, ID: newID("htask"),
	}
}

// ─── world state + belief ────────────────────────────────────────────────────

// Belief is a confidence-weighted view of one fact, fused from possibly-disagreeing observations.
type Belief struct {
	Value      any      `json:"value"`
	Confidence float64  `json:"confidence"`
	Sources    []string `json:"sources"`
	Conflict   bool     `json:"conflict"`
	UpdatedAt  float64  `json:"updated_at"`
}

// ─── mission ─────────────────────────────────────────────────────────────────

// Mission is the program. Holds a world-state ref and the ACTIVE plan id — never a graph directly.
type Mission struct {
	Goal         string         `json:"goal"`
	Constraints  []string       `json:"constraints"`
	PolicyRefs   []string       `json:"policy_refs"`
	Budget       Budget         `json:"budget"`
	Deadline     *float64       `json:"deadline"`
	State        MissionState   `json:"state"`
	WorldStateID string         `json:"world_state_id"`
	ActivePlanID string         `json:"active_plan_id"`
	Outcome      map[string]any `json:"outcome"`
	Template     string         `json:"template"`
	ID           string         `json:"id"`
	CreatedAt    float64        `json:"created_at"`
	UpdatedAt    float64        `json:"updated_at"`
}

// NewMission applies the Python defaults (state planning, default Budget, generated id/timestamps).
func NewMission(goal string) *Mission {
	t := now()
	return &Mission{
		Goal: goal, Budget: NewBudget(), State: MissionPlanning,
		WorldStateID: newID("world"), ID: newID("mission"), CreatedAt: t, UpdatedAt: t,
	}
}

// ─── learning ────────────────────────────────────────────────────────────────

type MissionOutcomeEvent struct {
	MissionID     string   `json:"mission_id"`
	Kind          string   `json:"kind"`
	Success       bool     `json:"success"`
	BusinessValue float64  `json:"business_value"`
	Cost          NodeCost `json:"cost"`
	PlanSignature string   `json:"plan_signature"`
	NodeID        string   `json:"node_id"`
}

// Lesson is the structured output of a Reflection pass — high-signal training data for planning.
type Lesson struct {
	MissionID           string   `json:"mission_id"`
	FailedAssumption    string   `json:"failed_assumption"`
	MissingVerification string   `json:"missing_verification"`
	BetterCapability    string   `json:"better_capability"`
	Evidence            []string `json:"evidence"`
	Success             bool     `json:"success"`
}

// ─── P7: evidence & verification plane (observed → verified → believed → committed) ──

type VerificationDecision string

const (
	VerAccept   VerificationDecision = "accept"   // may become authoritative world state
	VerEscalate VerificationDecision = "escalate" // unmet assertion / unbacked claim → a human decides
	VerReject   VerificationDecision = "reject"   // hard failure (malformed / permission)
)

// Claim is a statement a verification must substantiate before a result is accepted.
type Claim struct {
	Statement        string `json:"statement"`
	Subject          string `json:"subject"`
	RequiredByPolicy string `json:"required_by_policy"`
	ID               string `json:"id"`
}

func NewClaim(statement string) Claim { return Claim{Statement: statement, ID: newID("claim")} }

// EvidenceRecord is append-only evidence backing a claim.
type EvidenceRecord struct {
	ClaimID    string   `json:"claim_id"`
	Source     string   `json:"source"`
	Content    any      `json:"content"`
	ProducedAt float64  `json:"produced_at"`
	FreshnessS *float64 `json:"freshness_s"`
	ID         string   `json:"id"`
}

func NewEvidenceRecord(claimID, source string) EvidenceRecord {
	return EvidenceRecord{ClaimID: claimID, Source: source, ProducedAt: now(), ID: newID("ev")}
}

// StateAssertion is a checkable expectation about a result / world-state key.
type StateAssertion struct {
	Key      string `json:"key"`
	Op       string `json:"op"` // present | truthy | equals | in
	Expected any    `json:"expected"`
	Observed any    `json:"observed"`
	Holds    *bool  `json:"holds"`
}

type VerificationResult struct {
	Decision    VerificationDecision `json:"decision"`
	Confidence  float64              `json:"confidence"`
	Checks      []map[string]any     `json:"checks"` // {stage, passed, detail}
	EvidenceIDs []string             `json:"evidence_ids"`
	Verifier    string               `json:"verifier"`
	ID          string               `json:"id"`
}

func NewVerificationResult(decision VerificationDecision) VerificationResult {
	return VerificationResult{Decision: decision, Confidence: 1.0, Verifier: "composite", ID: newID("vr")}
}

// ─── P8: policy & human-decision plane (approval is a decision, not a static flag) ──

type ApprovalRule string

const (
	RuleMandatory     ApprovalRule = "mandatory_capability"
	RuleMissionPolicy ApprovalRule = "mission_policy"
	RuleDynamicRisk   ApprovalRule = "dynamic_risk"
	RuleVerification  ApprovalRule = "verification_escalation"
)

// RiskFactors are the dimensions a single risk×value threshold is too reductive to capture (P8).
type RiskFactors struct {
	Reversibility        float64 `json:"reversibility"`
	Uncertainty          float64 `json:"uncertainty"`
	Monetary             float64 `json:"monetary"`
	Novelty              float64 `json:"novelty"`
	BlastRadius          float64 `json:"blast_radius"`
	VerificationCoverage float64 `json:"verification_coverage"`
	Permissioned         bool    `json:"permissioned"`
	Regulatory           bool    `json:"regulatory"`
	Score                float64 `json:"score"`
}

// NewRiskFactors mirrors the Python defaults (fully reversible, permissioned, full coverage).
func NewRiskFactors() RiskFactors {
	return RiskFactors{Reversibility: 1.0, VerificationCoverage: 1.0, Permissioned: true}
}

// ApprovalDecision is the policy plane's output: whether a human gate is required and why.
type ApprovalDecision struct {
	Required bool           `json:"required"`
	Rules    []string       `json:"rules"`
	Risk     RiskFactors    `json:"risk"`
	Packet   map[string]any `json:"packet"`
	ID       string         `json:"id"`
}

func NewApprovalDecision(required bool) ApprovalDecision {
	return ApprovalDecision{Required: required, Risk: NewRiskFactors(), ID: newID("apd")}
}

// jsonable renders any value as a generic JSON view (map/slice/scalar), the Go analogue of the
// Python to_jsonable dataclass walk — used for event payloads and the API.
func jsonable(v any) any {
	b, err := json.Marshal(v)
	if err != nil {
		return nil
	}
	var out any
	_ = json.Unmarshal(b, &out)
	return out
}
