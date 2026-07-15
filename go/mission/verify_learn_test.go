package mission

import "testing"

func verifyFleet() *CapabilityRegistry {
	r := NewCapabilityRegistry(nil)
	c := NewCapabilitySpec("op.act", "op")
	c.SideEffecting = true
	c.Outputs = map[string]string{"receipt": "string"}
	c.Permissions = []string{"op:write"}
	r.Register(CapabilityManifest{Operator: "op", Capabilities: []CapabilitySpec{c}})
	return r
}

func starGrants(*Mission) map[string]bool { return map[string]bool{"*": true} }

func TestVerifyAcceptRejectEscalate(t *testing.T) {
	v := NewCompositeVerifier(verifyFleet(), starGrants)
	node := NewNode("op.act", "op")
	node.SideEffecting = true

	// ACCEPT: result carries the declared output
	if vr := v.Verify(&node, map[string]any{"receipt": "r1"}, nil, NewMission("m")); vr.Decision != VerAccept {
		t.Fatalf("clean result should ACCEPT: %v", vr.Decision)
	}
	// REJECT: malformed result (missing declared output)
	if vr := v.Verify(&node, map[string]any{"ok": true}, nil, NewMission("m")); vr.Decision != VerReject {
		t.Fatalf("malformed result should REJECT: %v", vr.Decision)
	}
	// ESCALATE: an unmet assertion
	asserted := NewNode("op.act", "op")
	asserted.SideEffecting = true
	asserted.Assertions = []StateAssertion{{Key: "approved", Op: "truthy"}}
	if vr := v.Verify(&asserted, map[string]any{"receipt": "r1", "approved": false}, nil, NewMission("m")); vr.Decision != VerEscalate {
		t.Fatalf("unmet assertion should ESCALATE: %v", vr.Decision)
	}
}

func TestVerifyRejectsLostPermission(t *testing.T) {
	v := NewCompositeVerifier(verifyFleet(), func(*Mission) map[string]bool { return map[string]bool{} })
	node := NewNode("op.act", "op")
	node.SideEffecting = true
	if vr := v.Verify(&node, map[string]any{"receipt": "r1"}, nil, NewMission("m")); vr.Decision != VerReject {
		t.Fatalf("revoked permission should REJECT: %v", vr.Decision)
	}
}

func TestEvidenceLedger(t *testing.T) {
	s := NewEventStore("")
	log := NewEvidenceLog(s)
	claim := NewClaim("the invoice was paid")
	log.RaiseClaim("m", claim)
	log.RecordEvidence("m", NewEvidenceRecord(claim.ID, "billing"))
	log.RecordVerification("m", "n1", NewVerificationResult(VerAccept))
	led := log.Ledger("m")
	if len(led) != 3 || led[0]["type"] != "ClaimRaised" || led[2]["node_id"] != "n1" {
		t.Fatalf("ledger = %v", led)
	}
}

func TestLearningFourLoopsAndConfidence(t *testing.T) {
	r := verifyFleet()
	lr := NewLearningRouter(r)
	lr.RecordCapability("op.act", false)
	lr.RecordCapability("op.act", false)
	// learned confidence is written back to the manifest (feeds the planner)
	if r.Get("op.act").Confidence != 0.0 {
		t.Fatalf("confidence should fall to 0.0: %v", r.Get("op.act").Confidence)
	}
	if c, ok := lr.CapabilityConfidence("op.act"); !ok || c != 0.0 {
		t.Fatalf("CapabilityConfidence = %v,%v", c, ok)
	}
	lr.RecordVerification("op.act", true)
	lr.Record(MissionOutcomeEvent{MissionID: "m", Kind: "onboarding", Success: true, BusinessValue: 100, PlanSignature: "a->b"})
	pol := lr.Policy()
	loops := pol["capability"].(map[string]any)
	if _, ok := loops["op.act"]; !ok {
		t.Fatal("capability loop missing")
	}
	if pol["business"].(map[string]any)["onboarding"] != 100.0 {
		t.Fatalf("business loop = %v", pol["business"])
	}
	if _, ok := pol["verification"].(map[string]any)["op.act"]; !ok {
		t.Fatal("verification loop missing")
	}
}

func TestReflectProposesBetterCapability(t *testing.T) {
	r := NewCapabilityRegistry(nil)
	a := NewCapabilitySpec("provA.charge", "provA")
	a.Provides = []string{"charged"}
	b := NewCapabilitySpec("provB.charge", "provB")
	b.Provides = []string{"charged"}
	r.Register(CapabilityManifest{Operator: "provA", Capabilities: []CapabilitySpec{a}})
	r.Register(CapabilityManifest{Operator: "provB", Capabilities: []CapabilitySpec{b}})
	m := NewMission("charge")
	m.PolicyRefs = []string{"*"}
	intent := NewExecutionIntent(m.ID, NewIntentStep("charged", "charge the customer"))
	plan, _ := CompileIntent(m, intent, r, 1, "initial", nil)
	nid := plan.Graph.Nodes[0].ID
	timeline := []map[string]any{{"type": "NodeFailed", "payload": map[string]any{"node_id": nid, "capability": "provA.charge"}}}
	lesson := reflectMission(m.ID, false, timeline, r, plan)
	if lesson.Success || lesson.BetterCapability != "provB.charge" {
		t.Fatalf("reflection = %+v", lesson)
	}
}
