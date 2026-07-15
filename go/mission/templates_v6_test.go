package mission

import "testing"

// v5→v6 parity: templates.go must carry the same seven templates as the Python
// agentic_os/mission/templates.py (onboarding, invoice_recovery, deploy_app, teardown_app,
// cost_audit, revenue_rescue, product_launch), with the same step shape and gated steps.
func TestTemplateParityWithPython(t *testing.T) {
	steps := map[string]int{
		"onboarding": 4, "invoice_recovery": 3, "deploy_app": 5, "teardown_app": 2,
		"cost_audit": 7, "revenue_rescue": 4, "product_launch": 9,
	}
	for name, want := range steps {
		it, ok := getTemplate(name, "m1")
		if !ok {
			t.Fatalf("template %q not registered", name)
		}
		if got := len(it.Steps); got != want {
			t.Errorf("%s: %d steps, want %d", name, got, want)
		}
	}
	// each gated template's approval step carries a human-approval constraint (P8 gates on it)
	gated := map[string]string{
		"deploy_app": "infra_provisioned", "teardown_app": "infra_destroyed",
		"cost_audit": "risky_fixes_applied", "product_launch": "launch_published",
		"revenue_rescue": "dunning_attempted",
	}
	for tmpl, outcome := range gated {
		it, _ := getTemplate(tmpl, "m1")
		ok := false
		for _, s := range it.Steps {
			if s.Outcome == outcome && len(s.Constraints) > 0 {
				ok = true
			}
		}
		if !ok {
			t.Errorf("%s: gated step %q missing an approval constraint", tmpl, outcome)
		}
	}
	// product_launch is the 8-app fan-out: five prep outcomes feed the single publish gate
	pl, _ := getTemplate("product_launch", "m1")
	for _, s := range pl.Steps {
		if s.Outcome == "launch_published" && len(s.InputsFrom) != 5 {
			t.Errorf("product_launch publish should fan in from 5 prep steps, got %d", len(s.InputsFrom))
		}
	}
}
