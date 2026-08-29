package mission

// Canonical concurrency safety semantics (Go parity with Python test_concurrency_metadata.py):
// the scheduler parallelizes the maximal SAFE ready-set and serializes resource conflicts, with an
// auditable reason. 2B = bounded provider fan-out, 2C = conflicting shared resource (static + input-
// derived), plus read-only non-conflict and the EXPLAIN surface.

import "testing"

func cnode(id string, mode, keyTmpl string, keys []string, maxPar int, inputs map[string]any) Node {
	n := NewNode("cap."+id, "op")
	n.ID = id
	n.ConcurrencyMode = mode
	n.ConcurrencyKey = keyTmpl
	n.ResourceKeys = keys
	n.MaxParallelism = maxPar
	n.Inputs = inputs
	return n
}

func graphOf(nodes ...Node) *ExecutionGraph {
	g := NewExecutionGraph()
	g.Nodes = nodes
	return g
}

var wide = SchedulePolicy{MaxConcurrency: 8, IsBusinessHours: func() bool { return true }}

func releasedIDs(g *ExecutionGraph) map[string]bool {
	out := map[string]bool{}
	for _, n := range (TopoScheduler{}).Ready(g, map[string]bool{}, map[string]bool{}, wide) {
		out[n.ID] = true
	}
	return out
}

// 2C — same exclusive key serializes; a different key runs alongside.
func TestSameExclusiveKeySerializes(t *testing.T) {
	g := graphOf(
		cnode("A", ModeSideEffecting, "", []string{"k8s:cluster:prod"}, 0, nil),
		cnode("B", ModeSideEffecting, "", []string{"k8s:cluster:prod"}, 0, nil),
		cnode("C", ModeSideEffecting, "", []string{"k8s:cluster:staging"}, 0, nil),
	)
	rel := releasedIDs(g)
	if !rel["C"] {
		t.Fatalf("staging (different key) should run; got %v", rel)
	}
	if (rel["A"] && rel["B"]) || (!rel["A"] && !rel["B"]) {
		t.Fatalf("exactly one of the prod-conflicting pair should release; got %v", rel)
	}
}

// 2C input-derived: different accounts parallelize, same account serializes.
func TestInputDerivedKeys(t *testing.T) {
	diff := graphOf(
		cnode("A", ModeSideEffecting, "crm:account:{account_id}", nil, 0, map[string]any{"account_id": 123}),
		cnode("B", ModeSideEffecting, "crm:account:{account_id}", nil, 0, map[string]any{"account_id": 456}),
	)
	if rel := releasedIDs(diff); !(rel["A"] && rel["B"]) {
		t.Fatalf("different accounts must parallelize; got %v", rel)
	}
	same := graphOf(
		cnode("A", ModeSideEffecting, "crm:account:{account_id}", nil, 0, map[string]any{"account_id": 123}),
		cnode("B", ModeSideEffecting, "crm:account:{account_id}", nil, 0, map[string]any{"account_id": 123}),
	)
	if rel := releasedIDs(same); len(rel) != 1 {
		t.Fatalf("same account must serialize; got %v", rel)
	}
}

// unresolved template serializes conservatively.
func TestUnresolvedTemplateSerializes(t *testing.T) {
	g := graphOf(
		cnode("A", ModeSideEffecting, "crm:account:{account_id}", nil, 0, nil),
		cnode("B", ModeSideEffecting, "crm:account:{account_id}", nil, 0, nil),
	)
	if rel := releasedIDs(g); len(rel) != 1 {
		t.Fatalf("unresolved template must serialize conservatively; got %v", rel)
	}
}

// 2B — bounded provider fan-out capped by MaxParallelism.
func TestBoundedProviderFanout(t *testing.T) {
	var nodes []Node
	for _, id := range []string{"R0", "R1", "R2", "R3", "R4"} {
		nodes = append(nodes, cnode(id, ModeSideEffecting, "provider:seedance", nil, 2, nil))
	}
	if rel := releasedIDs(graphOf(nodes...)); len(rel) != 2 {
		t.Fatalf("provider fan-out must bound to 2; got %v", rel)
	}
}

// read-only never locks, even on a shared key.
func TestReadOnlyDoesNotLock(t *testing.T) {
	g := graphOf(
		cnode("A", ModeReadOnly, "", []string{"corpus:main"}, 0, nil),
		cnode("B", ModeReadOnly, "", []string{"corpus:main"}, 0, nil),
	)
	if rel := releasedIDs(g); !(rel["A"] && rel["B"]) {
		t.Fatalf("read-only nodes must not lock; got %v", rel)
	}
}

// capabilities that declare nothing are unchanged (bounded only by the cap).
func TestUnclassifiedUnchanged(t *testing.T) {
	var nodes []Node
	for _, id := range []string{"N0", "N1", "N2", "N3"} {
		nodes = append(nodes, cnode(id, "", "", nil, 0, nil))
	}
	if rel := releasedIDs(graphOf(nodes...)); len(rel) != 4 {
		t.Fatalf("unclassified caps must all release; got %v", rel)
	}
}

// EXPLAIN surface gives an auditable reason per node.
func TestExplainReason(t *testing.T) {
	g := graphOf(
		cnode("A", ModeSideEffecting, "", []string{"k8s:cluster:prod"}, 0, nil),
		cnode("B", ModeSideEffecting, "", []string{"k8s:cluster:prod"}, 0, nil),
	)
	rows := (TopoScheduler{}).Explain(g, map[string]bool{}, map[string]bool{}, wide)
	decisions := map[string]bool{}
	var serializedReason string
	for _, r := range rows {
		decisions[r.Decision] = true
		if r.Decision == "serialized" {
			serializedReason = r.Reason
		}
	}
	if !decisions["parallelized"] || !decisions["serialized"] {
		t.Fatalf("expected both decisions; got %v", decisions)
	}
	if serializedReason != "resource_key=k8s:cluster:prod at limit 1" {
		t.Fatalf("serialization reason not auditable: %q", serializedReason)
	}
}
