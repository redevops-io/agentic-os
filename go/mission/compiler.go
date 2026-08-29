package mission

// Execution Compiler — the PHYSICAL layer (logical plan → physical plan). NO LLM. Binds an
// ExecutionIntent to concrete capabilities (registry + cost model), orders them into a graph,
// injects human/permission decisions, assigns idempotency keys, and validates every node's
// permissions against the mission's grants (FAIL CLOSED). Go port of compiler.py.

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
)

// CompileError is raised when an intent can't be compiled (no capability, or a permission denied).
type CompileError struct{ Msg string }

func (e *CompileError) Error() string { return e.Msg }

// minBindScore: a discovery match below this similarity is NOT a real provider — binding an
// unrelated (but permitted) capability to a required outcome would be a silent correctness bug.
const minBindScore = 0.2

// nodeID is a deterministic node id so a graph RECOMPILED after a restart correlates with the
// event-sourced execution state (keyed by node id). Durability depends on this.
func nodeID(missionID string, revision int, outcome string) string {
	parts := strings.Split(missionID, "_")
	last := parts[len(parts)-1]
	if len(last) > 8 {
		last = last[:8]
	}
	return fmt.Sprintf("nd_%s_%d_%s", last, revision, slugify(outcome, 32))
}

func idemKey(missionID string, revision int, outcome string) string {
	h := sha256.Sum256([]byte(fmt.Sprintf("%s:%d:%s", missionID, revision, outcome)))
	return hex.EncodeToString(h[:])[:16]
}

func permittedCap(cap *CapabilitySpec, granted map[string]bool) bool {
	if granted["*"] {
		return true
	}
	for _, p := range cap.Permissions {
		if !granted[p] {
			return false
		}
	}
	return true
}

// bindCapability picks the best capability that PROVIDES the outcome, else a STRONG semantic match
// to the need. prefer (a capability name) forces a specific provider if it provides the outcome.
// Binding is a Context Runtime resolution: the compiler states the need and the runtime decides
// (mirrors compiler.py, which calls context.resolve(BIND_CAPABILITY)).
func bindCapability(stepNeed, outcome string, registry Registry, prefer string) *CapabilitySpec {
	b := NewLocalContextRuntime(registry).Resolve(ContextIntent{
		Kind: BindCapability, Need: stepNeed, Outcome: outcome, Prefer: prefer})
	cap, _ := b.Value.(*CapabilitySpec)
	return cap
}

// CompileIntent binds an intent to a physical ExecutionPlan (fail-closed on permissions).
func CompileIntent(mission *Mission, intent ExecutionIntent, registry Registry,
	revision int, reason string, prefer map[string]string) (*ExecutionPlan, error) {

	granted := map[string]bool{}
	for _, g := range mission.PolicyRefs {
		granted[g] = true
	}
	if prefer == nil {
		prefer = map[string]string{}
	}
	graph := NewExecutionGraph()
	outcomeToNode := map[string]string{}
	type sn struct {
		step IntentStep
		idx  int
	}
	var stepNodes []sn

	for _, step := range intent.Steps {
		cap := bindCapability(step.Need, step.Outcome, registry, prefer[step.Outcome])
		if cap == nil {
			return nil, &CompileError{Msg: fmt.Sprintf("no capability provides outcome %q (need: %s)", step.Outcome, step.Need)}
		}
		if !permittedCap(cap, granted) {
			var missing []string
			for _, p := range cap.Permissions {
				if !granted[p] {
					missing = append(missing, p)
				}
			}
			return nil, &CompileError{Msg: fmt.Sprintf("permission denied for %q: missing grant(s) %v", cap.Name, missing)}
		}
		human := cap.ApprovalRequired
		for _, c := range step.Constraints {
			lc := strings.ToLower(c)
			if strings.Contains(lc, "human") || strings.Contains(lc, "review") {
				human = true
				break
			}
		}
		node := NewNode(cap.Name, cap.Operator)
		node.Produces = step.Outcome
		node.ApprovalRequired = human
		node.SideEffecting = cap.SideEffecting
		node.Undo = cap.Undo
		node.IdempotencyKey = idemKey(mission.ID, revision, step.Outcome)
		node.Cost = cap.Cost
		// carry the capability's declared concurrency safety semantics onto the node (additive)
		node.ConcurrencyMode = cap.ConcurrencyMode
		node.ConcurrencyKey = cap.ConcurrencyKey
		node.ResourceKeys = append([]string(nil), cap.ResourceKeys...)
		node.MaxParallelism = cap.MaxParallelism
		node.IntentStepID = step.ID
		node.Inputs = map[string]any{}
		node.ID = nodeID(mission.ID, revision, step.Outcome) // deterministic (restart-safe)

		graph.Nodes = append(graph.Nodes, node)
		outcomeToNode[step.Outcome] = node.ID
		stepNodes = append(stepNodes, sn{step, len(graph.Nodes) - 1})
	}

	for _, s := range stepNodes {
		for _, up := range s.step.InputsFrom {
			if upID, ok := outcomeToNode[up]; ok {
				graph.Nodes[s.idx].DependsOn = append(graph.Nodes[s.idx].DependsOn, upID)
			}
			graph.Nodes[s.idx].Inputs[up] = map[string]any{"$from_world": up}
		}
	}

	if err := assertAcyclic(graph); err != nil {
		return nil, err
	}
	plan := NewExecutionPlan(mission.ID, intent.ID, graph)
	plan.Revision = revision
	plan.Reason = reason
	return plan, nil
}

// assertAcyclic is Kahn's algorithm — errors if the graph has a cycle.
func assertAcyclic(graph *ExecutionGraph) error {
	indeg := map[string]int{}
	adj := map[string][]string{}
	for i := range graph.Nodes {
		indeg[graph.Nodes[i].ID] = 0
	}
	for i := range graph.Nodes {
		n := &graph.Nodes[i]
		for _, dep := range n.DependsOn {
			indeg[n.ID]++
			adj[dep] = append(adj[dep], n.ID)
		}
	}
	var ready []string
	for nid, d := range indeg {
		if d == 0 {
			ready = append(ready, nid)
		}
	}
	seen := 0
	for len(ready) > 0 {
		cur := ready[len(ready)-1]
		ready = ready[:len(ready)-1]
		seen++
		for _, nxt := range adj[cur] {
			indeg[nxt]--
			if indeg[nxt] == 0 {
				ready = append(ready, nxt)
			}
		}
	}
	if seen != len(graph.Nodes) {
		return &CompileError{Msg: "execution graph has a cycle"}
	}
	return nil
}
