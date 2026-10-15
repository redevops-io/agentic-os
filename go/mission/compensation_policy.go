package mission

import (
	"os"
	"strings"
)

// Failure policies (docs/COMPENSATION.md). Mirrors agentic_os/mission/compensation_policy.py.
const (
	PolicyHaltAndReport      = "HALT_AND_REPORT"
	PolicyCompensateDeclared = "COMPENSATE_DECLARED"
	PolicyContinueSiblings   = "CONTINUE_SIBLINGS"
	// PolicyPreserveOnly: keep every successful effect on failure (report partial completion). The Python
	// runtime additionally cleans up temporary resources that declare disposition=CLEANUP (+ preconditions +
	// authorization); Go ignores per-action declarations, so for Go it is identical to HALT_AND_REPORT.
	PolicyPreserveOnly = "PRESERVE_ONLY"
)

// TemplateFailurePolicy: the kernel templates default to PRESERVE_ONLY (a failed workflow never auto-reverses
// successful actions: created accounts, deployed resources, provisioned infrastructure are kept and reported).
// Compensation is an explicit opt-in (Mission.FailurePolicy / AGENTIC_OS_DEFAULT_FAILURE_POLICY). Mirrors
// agentic_os/mission/templates.py (sky_deploy has no Go template yet; listed so the gate agrees).
var TemplateFailurePolicy = map[string]string{
	"onboarding": PolicyPreserveOnly,
	"deploy_app": PolicyPreserveOnly,
	"sky_deploy": PolicyPreserveOnly,
}

func parsePolicy(raw string) string {
	raw = strings.ToUpper(strings.TrimSpace(raw))
	switch raw {
	case "":
		return ""
	case PolicyHaltAndReport, PolicyCompensateDeclared, PolicyContinueSiblings, PolicyPreserveOnly:
		return raw
	}
	return PolicyHaltAndReport // unrecognised: fail SAFE, never widen into compensation
}

// EffectiveFailurePolicy resolves: the mission's own policy, the deployment-wide AGENTIC_OS_DEFAULT_FAILURE_POLICY
// (an explicit deployment decision; it beats the template table), the template default (kernel templates:
// PRESERVE_ONLY), then HALT_AND_REPORT. Mirrors compensation_policy.effective_policy/resolve_default.
func EffectiveFailurePolicy(m *Mission) string {
	if p := parsePolicy(m.FailurePolicy); p != "" {
		return p
	}
	if p := parsePolicy(os.Getenv("AGENTIC_OS_DEFAULT_FAILURE_POLICY")); p != "" {
		return p
	}
	if p := parsePolicy(TemplateFailurePolicy[m.Template]); p != "" {
		return p
	}
	return PolicyHaltAndReport
}
