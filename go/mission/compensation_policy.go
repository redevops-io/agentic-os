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
)

// TemplateFailurePolicy: templates that OPT IN to undoing completed steps that declare an undo capability.
var TemplateFailurePolicy = map[string]string{
	"onboarding": PolicyCompensateDeclared,
	"deploy_app": PolicyCompensateDeclared,
}

func parsePolicy(raw string) string {
	raw = strings.ToUpper(strings.TrimSpace(raw))
	switch raw {
	case "":
		return ""
	case PolicyHaltAndReport, PolicyCompensateDeclared, PolicyContinueSiblings:
		return raw
	}
	return PolicyHaltAndReport // unrecognised: fail SAFE, never widen into compensation
}

// EffectiveFailurePolicy resolves: the mission's own policy, its template default, the deployment-wide
// AGENTIC_OS_DEFAULT_FAILURE_POLICY escape hatch, then HALT_AND_REPORT.
func EffectiveFailurePolicy(m *Mission) string {
	if p := parsePolicy(m.FailurePolicy); p != "" {
		return p
	}
	if p := parsePolicy(TemplateFailurePolicy[m.Template]); p != "" {
		return p
	}
	if p := parsePolicy(os.Getenv("AGENTIC_OS_DEFAULT_FAILURE_POLICY")); p != "" {
		return p
	}
	return PolicyHaltAndReport
}
