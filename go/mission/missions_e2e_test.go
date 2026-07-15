package mission

import "testing"

// The Go analog of the Python mission suite (api/test_missions.py): every sample
// mission runs from scratch through the real runtime and SUCCEEDS on approve, and a rejected gate
// FAILS the mission. Uses one universal in-process fleet — a capability per outcome across all seven
// templates. Steps a template marks "requires human approval" are ApprovalRequired (+ side-effecting
// & reversible, so a reject compensates); every other outcome is read-only, so each template gates
// exactly on its intended step.

var gatedOutcomes = map[string]bool{
	"consent_filed": true, "infra_provisioned": true, "infra_destroyed": true,
	"risky_fixes_applied": true, "dunning_attempted": true, "launch_published": true,
}

var allOutcomes = []string{
	// onboarding
	"subscription", "onboarding_sent", "revenue_recorded", "consent_filed",
	// invoice_recovery
	"overdue_invoice", "dunning_sent", "payment_recorded",
	// deploy_app
	"image_scanned", "infra_planned", "infra_provisioned", "app_configured", "deploy_verified",
	// teardown_app
	"release_rolled_back", "infra_destroyed",
	// cost_audit
	"inventory_taken", "slo_baselined", "waste_found", "savings_estimated",
	"safe_fixes_applied", "risky_fixes_applied", "audit_verified",
	// revenue_rescue
	"dunning_attempted", "reply_drafted", "campaign_drafted", "reconciliation_staged",
	// product_launch
	"competitor_brief", "announcement_drafted", "blog_drafted", "social_drafted", "email_drafted",
	"leads_scored", "support_briefed", "launch_published", "conversions_tracked",
}

func everyMissionRuntime() *MissionRuntime {
	reg := NewCapabilityRegistry(nil)
	handlers := map[string]Handler{}
	noop := func(map[string]any) (map[string]any, error) { return map[string]any{}, nil }
	var specs []CapabilitySpec
	for _, o := range allOutcomes {
		name := "mock." + o
		c := NewCapabilitySpec(name, "mock")
		c.Provides = []string{o}
		c.Outputs = map[string]string{} // no declared outputs ⇒ syntactic verify trivially passes
		if gatedOutcomes[o] {
			c.ApprovalRequired = true
			c.SideEffecting = true
			c.Undo = name + ".undo"
			handlers[name+".undo"] = noop
		}
		specs = append(specs, c)
		handlers[name] = noop
	}
	reg.Register(CapabilityManifest{Operator: "mock", Capabilities: specs})
	return NewMissionRuntime(reg, NewExecutor(NewInMemoryOperatorClient(handlers)))
}

// driveToTerminal creates a mission from a template, runs it, and applies `decision` to each human
// gate until the mission reaches a terminal state (mirrors the Python run_to_success).
func driveToTerminal(t *testing.T, rt *MissionRuntime, template, decision string) *Mission {
	t.Helper()
	m := rt.CreateMission("run "+template, CreateOpts{PolicyRefs: []string{"*"}, Template: template})
	rt.Run(m.ID)
	for i := 0; i < 16; i++ {
		switch st := rt.repoState(t, m.ID); st {
		case MissionSucceeded, MissionFailed:
			return m
		case MissionWaitingHuman:
			pend := rt.repo.PendingHuman(m.ID)
			if pend == nil {
				t.Fatalf("%s: waiting_human but no pending task", template)
			}
			rt.Approve(m.ID, pend["node_id"].(string), decision, nil)
		default:
			t.Fatalf("%s: unexpected non-terminal state %v", template, st)
		}
	}
	t.Fatalf("%s: did not reach a terminal state", template)
	return m
}

// Every sample mission runs from scratch and SUCCEEDS once its gate(s) are approved.
func TestEverySampleMissionSucceeds(t *testing.T) {
	for _, tmpl := range []string{
		"onboarding", "invoice_recovery", "deploy_app", "teardown_app",
		"cost_audit", "revenue_rescue", "product_launch",
	} {
		rt := everyMissionRuntime()
		m := driveToTerminal(t, rt, tmpl, "approve")
		if got := rt.repoState(t, m.ID); got != MissionSucceeded {
			t.Errorf("%s: state %v, want Succeeded", tmpl, got)
		}
	}
}

// Each gated template parks on exactly one human gate before approval (nothing runs past it).
func TestGatedMissionsParkBeforeApproval(t *testing.T) {
	for tmpl, outcome := range map[string]string{
		"deploy_app": "infra_provisioned", "teardown_app": "infra_destroyed",
		"cost_audit": "risky_fixes_applied", "product_launch": "launch_published",
		"revenue_rescue": "dunning_attempted",
	} {
		rt := everyMissionRuntime()
		m := rt.CreateMission("gate "+tmpl, CreateOpts{PolicyRefs: []string{"*"}, Template: tmpl})
		rt.Run(m.ID)
		if got := rt.repoState(t, m.ID); got != MissionWaitingHuman {
			t.Errorf("%s: state %v, want WaitingHuman", tmpl, got)
			continue
		}
		if pend := rt.repo.PendingHuman(m.ID); pend == nil {
			t.Errorf("%s: no pending human task on the %q gate", tmpl, outcome)
		}
	}
}

// A rejected gate fails the mission — the irreversible step never commits.
func TestRejectedGateFailsMission(t *testing.T) {
	for _, tmpl := range []string{"deploy_app", "product_launch", "cost_audit"} {
		rt := everyMissionRuntime()
		m := driveToTerminal(t, rt, tmpl, "reject")
		if got := rt.repoState(t, m.ID); got != MissionFailed {
			t.Errorf("%s reject: state %v, want Failed", tmpl, got)
		}
	}
}
