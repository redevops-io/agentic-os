package mission

import (
	"math"
	"testing"
)

// ─── P8 policy decision ──────────────────────────────────────────────────────

func TestPolicyDecisionMultiFactor(t *testing.T) {
	r := NewCapabilityRegistry(nil)
	c := NewCapabilitySpec("op.act", "op")
	c.SideEffecting = true // irreversible (no undo)
	c.Confidence = 0.3     // low-confidence → uncertainty + novelty push risk over threshold
	c.Provides = []string{"done"}
	r.Register(CapabilityManifest{Operator: "op", Capabilities: []CapabilitySpec{c}})
	plane := NewPolicyDecisionPlane(r, nil, nil, nil)

	m := NewMission("act")
	m.PolicyRefs = []string{"*"}
	node := NewNode("op.act", "op")
	node.SideEffecting = true
	node.Produces = "done"
	graph := &ExecutionGraph{Nodes: []Node{node}}

	d := plane.Decide(&node, nil, m, graph)
	if !d.Required || !strContains(d.Rules, "dynamic_risk") || d.Risk.Reversibility != 0.0 {
		t.Fatalf("risky irreversible action should gate: %+v", d)
	}
	// a reversible, high-confidence action is not gated
	c2 := NewCapabilitySpec("op.safe", "op")
	c2.SideEffecting = true
	c2.Undo = "op.undo"
	c2.Provides = []string{"done"}
	r.Register(CapabilityManifest{Operator: "op", Capabilities: []CapabilitySpec{c2}})
	safe := NewNode("op.safe", "op")
	safe.SideEffecting = true
	safe.Undo = "op.undo"
	if plane.Decide(&safe, nil, m, &ExecutionGraph{Nodes: []Node{safe}}).Required {
		t.Fatal("reversible high-confidence action should not gate")
	}
}

func TestPolicyRegulatoryAndMissionPolicy(t *testing.T) {
	r := NewCapabilityRegistry(nil)
	c := NewCapabilitySpec("op.act", "op")
	c.Permissions = []string{"pii:write"}
	r.Register(CapabilityManifest{Operator: "op", Capabilities: []CapabilitySpec{c}})
	plane := NewPolicyDecisionPlane(r, nil, nil, nil)
	m := NewMission("act")
	m.PolicyRefs = []string{"pii:write"}
	node := NewNode("op.act", "op")
	if !plane.Decide(&node, nil, m, nil).Risk.Regulatory {
		t.Fatal("pii permission should mark regulatory")
	}
	// mission policy: approval:all forces a gate even for a trivial node
	m.Constraints = []string{"approval:all"}
	if !strContains(plane.Decide(&node, nil, m, nil).Rules, "mission_policy") {
		t.Fatal("approval:all should force a mission_policy gate")
	}
}

// ─── P9a active plan selection ───────────────────────────────────────────────

func TestActivePlannerEnumeratesCandidates(t *testing.T) {
	r := NewCapabilityRegistry(nil)
	for _, name := range []string{"provA.do", "provB.do"} {
		c := NewCapabilitySpec(name, "")
		c.Provides = []string{"done"}
		c.SideEffecting = true
		c.Undo = name + ".undo"
		r.Register(CapabilityManifest{Operator: name[:5], Capabilities: []CapabilitySpec{c}})
	}
	m := NewMission("act")
	m.PolicyRefs = []string{"*"}
	intent := NewExecutionIntent(m.ID, NewIntentStep("done", "do the thing"))
	ap := NewActivePlanner(r, NewSchedulePolicy(), nil)
	plan, cands, err := ap.Select(m, intent, 1, "initial")
	if err != nil || plan == nil || len(cands) < 2 {
		t.Fatalf("select = plan:%v cands:%d err:%v", plan != nil, len(cands), err)
	}
	sel := 0
	for _, c := range cands {
		if c.Selected {
			sel++
		}
	}
	if sel != 1 {
		t.Fatalf("exactly one candidate should be selected, got %d", sel)
	}
}

func TestScoreProjectionRejectsOverBudget(t *testing.T) {
	if !math.IsInf(scoreProjection(SimResult{WithinBudget: false}), -1) {
		t.Fatal("over-budget must score -Inf")
	}
	if scoreProjection(SimResult{WithinBudget: true, ExpectedSuccess: 1.0}) != 10.0 {
		t.Fatal("clean projection should score 10")
	}
}

// ─── P9b learners + promotion ladder ─────────────────────────────────────────

func TestLearnersRecommendAndPromote(t *testing.T) {
	stack := NewLearningStack()
	for i := 0; i < 5; i++ {
		stack.Routing.Observe("charge", "provA", false)
		stack.Routing.Observe("charge", "provB", true)
	}
	recs := stack.Recommendations()
	if len(recs) != 1 || recs[0].Recommended != "provB" || recs[0].Status != "recommended" {
		t.Fatalf("recommendation = %+v", recs)
	}
	if _, ok := stack.PromotedChoice("routing", "charge"); ok {
		t.Fatal("nothing promoted yet")
	}
	stack.Promote(&recs[0], "governance")
	if v, ok := stack.PromotedChoice("routing", "charge"); !ok || v != "provB" {
		t.Fatalf("promoted = %v,%v", v, ok)
	}
	if stack.Recommendations()[0].Status != "promoted" {
		t.Fatal("recommendation should now read promoted")
	}
}

// ─── P11 cross-mission scheduler ─────────────────────────────────────────────

func drain(s *CrossMissionScheduler) []string {
	var order []string
	for {
		adm := s.Admit()
		if len(adm) == 0 {
			break
		}
		for _, id := range adm {
			order = append(order, id)
			s.Release(id)
		}
	}
	return order
}

func TestCrossMissionFairShareAndPriority(t *testing.T) {
	s := NewCrossMissionScheduler(map[string]float64{"slots": 1}, 1.0)
	_ = s.Submit(ResourceRequest{MissionID: "A1", Demand: map[string]float64{"slots": 1}, Owner: "A"})
	_ = s.Submit(ResourceRequest{MissionID: "A2", Demand: map[string]float64{"slots": 1}, Owner: "A"})
	_ = s.Submit(ResourceRequest{MissionID: "B1", Demand: map[string]float64{"slots": 1}, Owner: "B"})
	if got := drain(s); len(got) != 3 || got[0] != "A1" || got[1] != "B1" || got[2] != "A2" {
		t.Fatalf("fair-share order = %v", got) // A served once → B jumps ahead of A2
	}

	p := NewCrossMissionScheduler(map[string]float64{"slots": 1}, 1.0)
	_ = p.Submit(ResourceRequest{MissionID: "low", Demand: map[string]float64{"slots": 1}, Priority: 0})
	_ = p.Submit(ResourceRequest{MissionID: "high", Demand: map[string]float64{"slots": 1}, Priority: 5})
	if got := drain(p); got[0] != "high" {
		t.Fatalf("priority order = %v", got)
	}
}

func TestInfeasibleRequestRejected(t *testing.T) {
	s := NewCrossMissionScheduler(map[string]float64{"slots": 2}, 1.0)
	if err := s.Submit(ResourceRequest{MissionID: "huge", Demand: map[string]float64{"slots": 3}}); err == nil {
		t.Fatal("demand > capacity must be rejected")
	}
}

// ─── P10 governance read-model ───────────────────────────────────────────────

func TestGovernancePolicyVersionAndInterventions(t *testing.T) {
	st := NewEventStore("")
	g := NewGovernanceLog(st)
	st.Append("MissionCreated", "m", map[string]any{"goal": "x"})
	if g.PolicyVersion("m") != 1 {
		t.Fatal("version starts at 1")
	}
	st.Append("PolicyChanged", "m", map[string]any{"actor": "admin", "new_version": 2})
	st.Append("MissionSuspended", "m", map[string]any{"actor": "oncall"})
	if g.PolicyVersion("m") != 2 {
		t.Fatalf("version after one change = %d", g.PolicyVersion("m"))
	}
	if len(g.Interventions("m")) != 2 {
		t.Fatalf("interventions = %v", g.Interventions("m"))
	}
}
