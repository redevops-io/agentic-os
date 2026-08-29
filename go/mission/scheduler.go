package mission

// Scheduler — decides WHEN and WHERE ready nodes run (separate from the Executor). The default is
// cost-aware topological waves honouring concurrency / per-operator / business-hours / approval
// batching. The scheduler never calls the model. Go port of scheduler.py.

import "sort"

// SchedulePolicy configures the scheduler.
type SchedulePolicy struct {
	MaxConcurrency    int
	PerOperatorLimit  map[string]int  // operator → max in-flight
	BusinessHoursOnly map[string]bool // capabilities gated to business hours
	IsBusinessHours   func() bool
	BatchApprovals    bool
}

// NewSchedulePolicy applies the Python defaults (concurrency 4, always business hours, batching on).
func NewSchedulePolicy() SchedulePolicy {
	return SchedulePolicy{MaxConcurrency: 4, IsBusinessHours: func() bool { return true }, BatchApprovals: true}
}

func (p SchedulePolicy) businessHours() bool {
	if p.IsBusinessHours == nil {
		return true
	}
	return p.IsBusinessHours()
}

// Scheduler releases the next wave of ready nodes.
type Scheduler interface {
	Ready(graph *ExecutionGraph, done, running map[string]bool, policy SchedulePolicy) []*Node
}

// TopoScheduler emits the ready frontier, highest-value nodes first, honouring policy.
type TopoScheduler struct{}

// frontier is the causally-ready, value-sorted candidate set (Phase A) — shared by Ready and Explain so
// both reason about the exact same ordered nodes.
func (TopoScheduler) frontier(graph *ExecutionGraph, done, running map[string]bool, policy SchedulePolicy) []*Node {
	var frontier []*Node
	for i := range graph.Nodes {
		n := &graph.Nodes[i]
		if done[n.ID] || running[n.ID] {
			continue
		}
		switch n.Status {
		case NodeDone, NodeSkipped, NodeCompensated, NodeFailed:
			continue
		}
		ok := true
		for _, dep := range n.DependsOn {
			if !done[dep] { // completed (done/skipped/compensated) unblock dependents
				ok = false
				break
			}
		}
		if ok {
			frontier = append(frontier, n)
		}
	}

	if !policy.businessHours() {
		var kept []*Node
		for _, n := range frontier {
			if !policy.BusinessHoursOnly[n.Capability] {
				kept = append(kept, n)
			}
		}
		frontier = kept
	}

	// two stable sorts mirroring Python: primary = value desc; secondary tie-break = (non-approval,
	// -usd) ascending applied first so it survives as the stable order under the value sort.
	sort.SliceStable(frontier, func(i, j int) bool {
		ai, aj := b2i(!frontier[i].ApprovalRequired), b2i(!frontier[j].ApprovalRequired)
		if ai != aj {
			return ai < aj
		}
		return -frontier[i].Cost.USD < -frontier[j].Cost.USD
	})
	sort.SliceStable(frontier, func(i, j int) bool {
		return schedValue(frontier[i]) > schedValue(frontier[j])
	})
	return frontier
}

func (t TopoScheduler) Ready(graph *ExecutionGraph, done, running map[string]bool, policy SchedulePolicy) []*Node {
	released, _ := t.release(t.frontier(graph, done, running, policy), graph, running, policy)
	return released
}

// release — Phase C: dispatch the maximal SAFE subset of the ready frontier. A node is held (and its
// reason recorded) when it would exceed the global cap, a per-operator limit, OR a resource-conflict key
// already in flight (nodes sharing an exclusive key serialize). held maps a held node id → the reason.
func (TopoScheduler) release(frontier []*Node, graph *ExecutionGraph, running map[string]bool, policy SchedulePolicy) ([]*Node, map[string]string) {
	held := map[string]string{}
	opInflight := map[string]int{}
	for _, op := range runningOps(graph, running) {
		opInflight[op]++
	}
	keyInflight := map[string]int{}
	for i := range graph.Nodes {
		if running[graph.Nodes[i].ID] {
			acquire(&graph.Nodes[i], keyInflight)
		}
	}
	capacity := policy.MaxConcurrency - len(running)
	if capacity < 0 {
		capacity = 0
	}
	var released []*Node
	for _, n := range frontier {
		if len(released) >= capacity {
			if _, ok := held[n.ID]; !ok {
				held[n.ID] = "concurrency cap " + itoa(policy.MaxConcurrency) + " reached"
			}
			continue
		}
		if limit, ok := policy.PerOperatorLimit[n.Operator]; ok && opInflight[n.Operator] >= limit {
			held[n.ID] = "operator '" + n.Operator + "' at limit " + itoa(limit)
			continue
		}
		if reason := conflict(n, keyInflight); reason != "" {
			held[n.ID] = reason
			continue
		}
		released = append(released, n)
		opInflight[n.Operator]++
		acquire(n, keyInflight)
	}
	return released, held
}

// ExplainRow is one node's scheduling decision this wave (the parallelism EXPLAIN surface).
type ExplainRow struct {
	Node         string   `json:"node"`
	Capability   string   `json:"capability"`
	Decision     string   `json:"decision"` // "parallelized" | "serialized"
	Reason       string   `json:"reason"`
	ResourceKeys []string `json:"resource_keys"`
}

// Explain returns, per ready node, whether it was released to run concurrently this wave or serialized,
// and why — turning scheduler behaviour into auditable runtime behaviour without changing what runs.
func (t TopoScheduler) Explain(graph *ExecutionGraph, done, running map[string]bool, policy SchedulePolicy) []ExplainRow {
	frontier := t.frontier(graph, done, running, policy)
	released, held := t.release(frontier, graph, running, policy)
	releasedIDs := map[string]bool{}
	for _, n := range released {
		releasedIDs[n.ID] = true
	}
	rows := make([]ExplainRow, 0, len(frontier))
	for _, n := range frontier {
		if releasedIDs[n.ID] {
			reason := "independent dependencies, no resource conflict"
			if n.ConcurrencyMode == ModeReadOnly {
				reason = "read-only capability"
			}
			rows = append(rows, ExplainRow{Node: n.ID, Capability: n.Capability, Decision: "parallelized",
				Reason: reason, ResourceKeys: resourceKeys(n)})
		} else {
			reason := held[n.ID]
			if reason == "" {
				reason = "held this wave"
			}
			rows = append(rows, ExplainRow{Node: n.ID, Capability: n.Capability, Decision: "serialized",
				Reason: reason, ResourceKeys: resourceKeys(n)})
		}
	}
	return rows
}

// schedValue: cheaper + non-approval nodes float up (real value comes from the cost model).
func schedValue(n *Node) float64 {
	base := 1.0
	if n.ApprovalRequired {
		base -= 0.1
	}
	return base - n.Cost.USD*0.01
}

func runningOps(graph *ExecutionGraph, running map[string]bool) []string {
	var out []string
	for i := range graph.Nodes {
		if running[graph.Nodes[i].ID] {
			out = append(out, graph.Nodes[i].Operator)
		}
	}
	return out
}
