package mission

import "testing"

func onboardingRegistry() *CapabilityRegistry {
	r := NewCapabilityRegistry(nil)
	mk := func(name, provides string, side bool) CapabilitySpec {
		c := NewCapabilitySpec(name, "")
		c.Provides = []string{provides}
		c.SideEffecting = side
		return c
	}
	r.Register(CapabilityManifest{Operator: "billing", Capabilities: []CapabilitySpec{
		mk("billing.create_subscription", "subscription", true)}})
	r.Register(CapabilityManifest{Operator: "support", Capabilities: []CapabilitySpec{
		mk("support.send_onboarding", "onboarding_sent", true)}})
	r.Register(CapabilityManifest{Operator: "books", Capabilities: []CapabilitySpec{
		mk("books.record_revenue", "revenue_recorded", true)}})
	r.Register(CapabilityManifest{Operator: "compliance", Capabilities: []CapabilitySpec{
		mk("compliance.file_consent", "consent_filed", true)}})
	return r
}

func TestTemplatePlannerMatchesOnboarding(t *testing.T) {
	intent := TemplatePlanner{}.Plan("mission_abc", "onboard a new customer", nil)
	if len(intent.Steps) != 4 || intent.Steps[0].Outcome != "subscription" {
		t.Fatalf("onboarding intent = %+v", intent.Steps)
	}
	// generic fallback for an unmatched goal
	g := TemplatePlanner{}.Plan("mission_abc", "do a random thing", nil)
	if len(g.Steps) != 1 || g.Steps[0].Outcome != "do_a_random_thing" {
		t.Fatalf("generic fallback = %+v", g.Steps)
	}
}

func TestCompileIntentBindsDepsAndDeterministicIDs(t *testing.T) {
	m := NewMission("onboard")
	m.PolicyRefs = []string{"*"}
	intent := templateOnboarding(m.ID)
	plan, err := CompileIntent(m, intent, onboardingRegistry(), 1, "initial", nil)
	if err != nil {
		t.Fatalf("compile error: %v", err)
	}
	if len(plan.Graph.Nodes) != 4 {
		t.Fatalf("nodes = %d", len(plan.Graph.Nodes))
	}
	// consent_filed depends on revenue_recorded; its node was compiled with a human gate (constraint)
	var consent *Node
	for i := range plan.Graph.Nodes {
		if plan.Graph.Nodes[i].Produces == "consent_filed" {
			consent = &plan.Graph.Nodes[i]
		}
	}
	if consent == nil || len(consent.DependsOn) != 1 || !consent.ApprovalRequired {
		t.Fatalf("consent node = %+v", consent)
	}
	// deterministic node id: recompiling the same intent yields identical ids
	plan2, _ := CompileIntent(m, intent, onboardingRegistry(), 1, "initial", nil)
	if plan.Graph.Nodes[0].ID != plan2.Graph.Nodes[0].ID {
		t.Fatal("node ids must be deterministic across recompiles")
	}
}

func TestCompileFailsClosedOnPermission(t *testing.T) {
	m := NewMission("onboard")
	m.PolicyRefs = []string{} // no grants
	r := NewCapabilityRegistry(nil)
	c := NewCapabilitySpec("billing.create_subscription", "")
	c.Provides = []string{"subscription"}
	c.Permissions = []string{"billing:write"}
	r.Register(CapabilityManifest{Operator: "billing", Capabilities: []CapabilitySpec{c}})
	intent := NewExecutionIntent(m.ID, NewIntentStep("subscription", "create a subscription"))
	scoped := NewPolicyScopedRegistry(r, m.PolicyRefs)
	if _, err := CompileIntent(m, intent, scoped, 1, "initial", nil); err == nil {
		t.Fatal("compile must fail closed when the only provider is scoped out")
	}
}

func TestSchedulerReleasesReadyFrontier(t *testing.T) {
	m := NewMission("onboard")
	m.PolicyRefs = []string{"*"}
	plan, _ := CompileIntent(m, templateOnboarding(m.ID), onboardingRegistry(), 1, "initial", nil)
	sched := TopoScheduler{}
	ready := sched.Ready(plan.Graph, map[string]bool{}, map[string]bool{}, NewSchedulePolicy())
	// only the root (subscription) has no deps
	if len(ready) != 1 || ready[0].Produces != "subscription" {
		t.Fatalf("initial ready = %v", ready)
	}
	// after subscription completes, its two dependents unblock
	done := map[string]bool{ready[0].ID: true}
	next := sched.Ready(plan.Graph, done, map[string]bool{}, NewSchedulePolicy())
	if len(next) != 2 {
		t.Fatalf("second wave = %d nodes", len(next))
	}
}

func TestSimulateProjectsAndGatesBudget(t *testing.T) {
	m := NewMission("onboard")
	m.PolicyRefs = []string{"*"}
	plan, _ := CompileIntent(m, templateOnboarding(m.ID), onboardingRegistry(), 1, "initial", nil)
	pol := NewSchedulePolicy()
	proj := Simulate(plan, m, onboardingRegistry(), &pol)
	if !proj.WithinBudget || proj.ExpectedSuccess <= 0 || proj.ExpectedApprovals != 1 {
		t.Fatalf("projection = %+v", proj)
	}
	// a tiny budget gates the plan
	m.Budget = Budget{USD: 0.0, HumanMinutes: 0.0}
	if Simulate(plan, m, onboardingRegistry(), &pol).WithinBudget {
		t.Fatal("zero budget should gate the plan")
	}
}
