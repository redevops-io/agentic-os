package mission

import "testing"

// A full onboarding fleet with in-process operators (side-effecting, some reversible).
func onboardingRuntime(t *testing.T) (*MissionRuntime, *CapabilityRegistry) {
	t.Helper()
	reg := NewCapabilityRegistry(nil)
	mk := func(name, op, provides string, undo string) CapabilitySpec {
		c := NewCapabilitySpec(name, op)
		c.Provides = []string{provides}
		c.SideEffecting = true
		c.Undo = undo
		c.Outputs = map[string]string{} // no declared outputs → syntactic check trivially passes
		return c
	}
	reg.Register(CapabilityManifest{Operator: "billing", Capabilities: []CapabilitySpec{mk("billing.create_subscription", "billing", "subscription", "billing.cancel")}})
	reg.Register(CapabilityManifest{Operator: "support", Capabilities: []CapabilitySpec{mk("support.send_onboarding", "support", "onboarding_sent", "")}})
	reg.Register(CapabilityManifest{Operator: "books", Capabilities: []CapabilitySpec{mk("books.record_revenue", "books", "revenue_recorded", "books.reverse")}})
	reg.Register(CapabilityManifest{Operator: "compliance", Capabilities: []CapabilitySpec{mk("compliance.file_consent", "compliance", "consent_filed", "")}})

	handlers := map[string]Handler{
		"billing.create_subscription": func(map[string]any) (map[string]any, error) { return map[string]any{"subscription_id": "sub_1"}, nil },
		"support.send_onboarding":     func(map[string]any) (map[string]any, error) { return map[string]any{"sent": true}, nil },
		"books.record_revenue":        func(map[string]any) (map[string]any, error) { return map[string]any{"entry": "je_1"}, nil },
		"compliance.file_consent":     func(map[string]any) (map[string]any, error) { return map[string]any{"filed": true}, nil },
		"billing.cancel":              func(map[string]any) (map[string]any, error) { return map[string]any{"cancelled": true}, nil },
		"books.reverse":               func(map[string]any) (map[string]any, error) { return map[string]any{"reversed": true}, nil },
	}
	rt := NewMissionRuntime(reg, NewExecutor(NewInMemoryOperatorClient(handlers)))
	return rt, reg
}

// End-to-end: onboarding runs to a human gate (consent is human-reviewed), then completes on approve.
func TestOnboardingEndToEndWithHumanGate(t *testing.T) {
	rt, _ := onboardingRuntime(t)
	m := rt.CreateMission("onboard a new customer", CreateOpts{PolicyRefs: []string{"*"}, Template: "onboarding"})
	rt.Run(m.ID)
	if rt.repoState(t, m.ID) != MissionWaitingHuman {
		t.Fatalf("should park on the consent human gate, got %v", m.State)
	}
	pend := rt.repo.PendingHuman(m.ID)
	if pend == nil {
		t.Fatal("expected a pending human task")
	}
	rt.Approve(m.ID, pend["node_id"].(string), "approve", nil)
	if rt.repoState(t, m.ID) != MissionSucceeded {
		t.Fatalf("mission should succeed after approval, got %v", rt.repoState(t, m.ID))
	}
	// world state carries the produced outcomes
	ex := rt.Explain(m.ID)
	world := ex["world_state"].(map[string]any)
	if world["subscription"] == nil || world["consent_filed"] == nil {
		t.Fatalf("world state incomplete: %v", world)
	}
}

func (rt *MissionRuntime) repoState(t *testing.T, mid string) MissionState {
	t.Helper()
	st, _ := rt.repo.State(mid)
	return st
}

// A failing side-effecting node triggers a saga (compensate the completed reversible one) + fail.
func TestSagaCompensationOnFailure(t *testing.T) {
	reg := NewCapabilityRegistry(nil)
	a := NewCapabilitySpec("op.a", "op")
	a.Provides = []string{"a"}
	a.SideEffecting = true
	a.Undo = "op.a_undo"
	b := NewCapabilitySpec("op.b", "op")
	b.Provides = []string{"b"}
	b.SideEffecting = true
	reg.Register(CapabilityManifest{Operator: "op", Capabilities: []CapabilitySpec{a, b}})

	handlers := map[string]Handler{
		"op.a":      func(map[string]any) (map[string]any, error) { return map[string]any{"ok": "a"}, nil },
		"op.a_undo": func(map[string]any) (map[string]any, error) { return map[string]any{"undone": true}, nil },
		"op.b":      func(map[string]any) (map[string]any, error) { return nil, &OperatorError{"gateway down"} },
	}
	rt := NewMissionRuntime(reg, NewExecutor(NewInMemoryOperatorClient(handlers)))
	m := rt.CreateMission("job", CreateOpts{PolicyRefs: []string{"*"}})
	// hand-author an intent: a then b(depends on a). ("job" has no template → the default plan
	// fails closed; inject a real plan and reset state so Run proceeds.)
	rt.plans[m.ID] = mustCompile(t, m, reg, "a", "b")
	m.State = MissionPlanning
	rt.Run(m.ID)
	if rt.repoState(t, m.ID) != MissionFailed {
		t.Fatalf("mission should fail when op.b errors, got %v", rt.repoState(t, m.ID))
	}
	comp := false
	for _, e := range rt.store.ForMission(m.ID) {
		if e.Type == "NodeCompensated" && e.Payload["undo"] == "op.a_undo" {
			comp = true
		}
	}
	if !comp {
		t.Fatal("the completed reversible node op.a should have been compensated")
	}
}

// v5: a side-effecting node whose OWN result is REJECTED compensates its own committed effect —
// the operator ran (the effect happened) but the result never became authoritative world state.
func TestRejectedNodeCompensatesOwnEffect(t *testing.T) {
	reg := NewCapabilityRegistry(nil)
	act := NewCapabilitySpec("op.act", "op")
	act.Provides = []string{"acted"}
	act.SideEffecting = true
	act.Undo = "op.act_undo"
	act.Outputs = map[string]string{"receipt": "string"} // a missing declared output → malformed → REJECT
	reg.Register(CapabilityManifest{Operator: "op", Capabilities: []CapabilitySpec{act}})

	handlers := map[string]Handler{
		"op.act":      func(map[string]any) (map[string]any, error) { return map[string]any{"did": "work"}, nil }, // no "receipt"
		"op.act_undo": func(map[string]any) (map[string]any, error) { return map[string]any{"undone": true}, nil },
	}
	rt := NewMissionRuntime(reg, NewExecutor(NewInMemoryOperatorClient(handlers)),
		WithVerifier(NewCompositeVerifier(reg, starGrants)))
	m := rt.CreateMission("job", CreateOpts{PolicyRefs: []string{"*"}})
	rt.plans[m.ID] = mustCompile(t, m, reg, "acted")
	m.State = MissionPlanning
	rt.Run(m.ID)

	if rt.repoState(t, m.ID) != MissionFailed {
		t.Fatalf("a rejected node should fail the mission, got %v", rt.repoState(t, m.ID))
	}
	comp := false
	for _, e := range rt.store.ForMission(m.ID) {
		if e.Type == "NodeCompensated" && e.Payload["undo"] == "op.act_undo" {
			comp = true
		}
	}
	if !comp {
		t.Fatal("the rejected side-effecting node op.act should compensate its OWN effect (v5)")
	}
}

func mustCompile(t *testing.T, m *Mission, reg Registry, outcomes ...string) *ExecutionPlan {
	t.Helper()
	steps := make([]IntentStep, 0, len(outcomes))
	for i, o := range outcomes {
		s := NewIntentStep(o, "do "+o)
		if i > 0 {
			s.InputsFrom = []string{outcomes[i-1]}
		}
		steps = append(steps, s)
	}
	intent := NewExecutionIntent(m.ID, steps...)
	plan, err := CompileIntent(m, intent, NewPolicyScopedRegistry(reg, m.PolicyRefs), 1, "initial", nil)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	return plan
}

// Capability learning closes the loop: a flaky provider's confidence drops and the planner switches.
func TestCapabilityLearningShiftsProvider(t *testing.T) {
	newFleet := func() (*CapabilityRegistry, *InMemoryOperatorClient) {
		reg := NewCapabilityRegistry(nil)
		provA := NewCapabilitySpec("provA.charge", "provA")
		provA.Provides = []string{"charged"}
		provA.SideEffecting = true
		provA.EstimatedValue = "high" // ranks first cold
		provB := NewCapabilitySpec("provB.charge", "provB")
		provB.Provides = []string{"charged"}
		provB.SideEffecting = true
		provB.EstimatedValue = "medium"
		reg.Register(CapabilityManifest{Operator: "provA", Capabilities: []CapabilitySpec{provA}})
		reg.Register(CapabilityManifest{Operator: "provB", Capabilities: []CapabilitySpec{provB}})
		client := NewInMemoryOperatorClient(map[string]Handler{
			"provA.charge": func(map[string]any) (map[string]any, error) { return nil, &OperatorError{"flaky"} },
			"provB.charge": func(map[string]any) (map[string]any, error) { return map[string]any{"charge_id": "ch1"}, nil },
		})
		return reg, client
	}
	reg, client := newFleet()
	rt := NewMissionRuntime(reg, NewExecutor(client))
	m1 := rt.CreateMission("charge", CreateOpts{PolicyRefs: []string{"*"}})
	if rt.plans[m1.ID].Graph.Nodes[0].Capability != "provA.charge" {
		t.Fatalf("cold start should pick provA, got %v", rt.plans[m1.ID].Graph.Nodes[0].Capability)
	}
	rt.Run(m1.ID)
	if rt.repoState(t, m1.ID) != MissionFailed || reg.Get("provA.charge").Confidence != 0.0 {
		t.Fatalf("provA should fail and be demoted: state=%v conf=%v", rt.repoState(t, m1.ID), reg.Get("provA.charge").Confidence)
	}
	// second mission: learning shifted the planner to the reliable provider
	m2 := rt.CreateMission("charge", CreateOpts{PolicyRefs: []string{"*"}})
	if rt.plans[m2.ID].Graph.Nodes[0].Capability != "provB.charge" {
		t.Fatalf("learned start should pick provB, got %v", rt.plans[m2.ID].Graph.Nodes[0].Capability)
	}
	rt.Run(m2.ID)
	if rt.repoState(t, m2.ID) != MissionSucceeded {
		t.Fatalf("m2 should succeed on provB, got %v", rt.repoState(t, m2.ID))
	}
}

// Restart durability: a fresh runtime on the same store rehydrates and resumes to completion.
func TestRestartResumeFromEventLog(t *testing.T) {
	rt, reg := onboardingRuntime(t)
	m := rt.CreateMission("onboard", CreateOpts{PolicyRefs: []string{"*"}, Template: "onboarding"})
	rt.Run(m.ID) // parks on the consent gate

	// a brand-new runtime on the SAME event store
	rt2 := NewMissionRuntime(reg, NewExecutor(NewInMemoryOperatorClient(map[string]Handler{
		"billing.create_subscription": func(map[string]any) (map[string]any, error) { return map[string]any{"subscription_id": "sub_1"}, nil },
		"support.send_onboarding":     func(map[string]any) (map[string]any, error) { return map[string]any{"sent": true}, nil },
		"books.record_revenue":        func(map[string]any) (map[string]any, error) { return map[string]any{"entry": "je_1"}, nil },
		"compliance.file_consent":     func(map[string]any) (map[string]any, error) { return map[string]any{"filed": true}, nil },
	})), WithStore(rt.store))
	rt2.Rehydrate(m.ID)
	pend := rt2.repo.PendingHuman(m.ID)
	if pend == nil {
		t.Fatal("rehydrated runtime should see the pending gate from the log")
	}
	rt2.Approve(m.ID, pend["node_id"].(string), "approve", nil)
	if st, _ := rt2.repo.State(m.ID); st != MissionSucceeded {
		t.Fatalf("resumed mission should complete, got %v", st)
	}
}
