package mission

import (
	"strings"
	"testing"
)

func TestNewIDShape(t *testing.T) {
	id := newID("mission")
	if !strings.HasPrefix(id, "mission_") || len(id) != len("mission_")+12 {
		t.Fatalf("newID = %q", id)
	}
	if newID("x") == newID("x") {
		t.Fatal("newID should be unique")
	}
}

func TestConstructorDefaults(t *testing.T) {
	m := NewMission("ship it")
	if m.State != MissionPlanning || m.Budget.USD != 5.0 || m.ID == "" || m.WorldStateID == "" {
		t.Fatalf("NewMission defaults wrong: %+v", m)
	}
	c := NewCapabilitySpec("crm.send", "crm")
	if !c.Deterministic || c.Confidence != 0.9 || c.EstimatedValue != "medium" {
		t.Fatalf("NewCapabilitySpec defaults wrong: %+v", c)
	}
	n := NewNode("crm.send", "crm")
	if n.Status != NodePending || n.ID == "" {
		t.Fatalf("NewNode defaults wrong: %+v", n)
	}
	if NewSimResult().ExpectedSuccess != 1.0 || !NewSimResult().WithinBudget {
		t.Fatal("NewSimResult defaults wrong")
	}
	if r := NewRiskFactors(); r.Reversibility != 1.0 || !r.Permissioned || r.VerificationCoverage != 1.0 {
		t.Fatalf("NewRiskFactors defaults wrong: %+v", r)
	}
}

func TestGraphByID(t *testing.T) {
	g := NewExecutionGraph()
	a := NewNode("a.do", "a")
	g.Nodes = append(g.Nodes, a)
	if got := g.ByID(a.ID); got == nil || got.Capability != "a.do" {
		t.Fatalf("ByID miss: %+v", got)
	}
	if g.ByID("nope") != nil {
		t.Fatal("ByID should return nil for unknown id")
	}
}

func TestEnumsSerializeToStrings(t *testing.T) {
	v := jsonable(NewVerificationResult(VerAccept))
	m, ok := v.(map[string]any)
	if !ok || m["decision"] != "accept" || m["verifier"] != "composite" {
		t.Fatalf("verification json = %#v", v)
	}
	// enum string identity
	if string(MissionWaitingHuman) != "waiting_human" || string(NodeCompensated) != "compensated" {
		t.Fatal("enum string values drifted")
	}
}

func TestJsonableNestedDataclass(t *testing.T) {
	m := NewMission("g")
	j := jsonable(m).(map[string]any)
	budget := j["budget"].(map[string]any)
	if budget["usd"].(float64) != 5.0 || j["state"] != "planning" {
		t.Fatalf("nested jsonable wrong: %#v", j)
	}
}
