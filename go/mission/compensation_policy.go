package mission

import (
	"fmt"
	"log"
	"os"
	"sort"
	"strings"
	"sync"
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

const (
	EnvDefaultFailurePolicy = "AGENTIC_OS_DEFAULT_FAILURE_POLICY"
	EnvReresolveLegacy     = "AGENTIC_OS_RERESOLVE_LEGACY_POLICY"

	SrcEnv, SrcEnvInvalid, SrcTemplate, SrcDefault = "env", "env_invalid", "template", "default"
)

var policyNames = []string{PolicyCompensateDeclared, PolicyContinueSiblings, PolicyHaltAndReport, PolicyPreserveOnly}

var (
	warnedMu sync.Mutex
	warned   = map[string]bool{}
)

func warnOnce(key, msg string) {
	warnedMu.Lock()
	seen := warned[key]
	warned[key] = true
	warnedMu.Unlock()
	if !seen {
		log.Print(msg)
	}
}

func isPolicyName(raw string) bool {
	raw = strings.ToUpper(strings.TrimSpace(raw))
	for _, p := range policyNames {
		if p == raw {
			return true
		}
	}
	return false
}

// pyStrList renders a []string like Python's repr(sorted(list)) so warnings are byte-identical across languages.
func pyStrList(xs []string) string {
	ys := append([]string{}, xs...)
	sort.Strings(ys)
	q := make([]string, len(ys))
	for i, y := range ys {
		q[i] = "'" + y + "'"
	}
	return "[" + strings.Join(q, ", ") + "]"
}

// ResolveDefaultDetailed -> (policy, source, warning). source is env | env_invalid | template | default. Mirrors
// compensation_policy.resolve_default_detailed: the deployment env beats the template table; an unrecognised env
// value fails SAFE to HALT_AND_REPORT (which also drops PRESERVE_ONLY's CLEANUP step) and says so loudly.
func ResolveDefaultDetailed(template string) (policy, source, warning string) {
	tpl := parsePolicy(TemplateFailurePolicy[template])
	raw := strings.TrimSpace(os.Getenv(EnvDefaultFailurePolicy))
	if raw != "" {
		env := parsePolicy(raw)
		if !isPolicyName(raw) {
			w := fmt.Sprintf("%s='%s' is not a failure policy %s: failing safe to HALT_AND_REPORT (nothing is compensated, not even PRESERVE_ONLY cleanup)",
				EnvDefaultFailurePolicy, raw, pyStrList(policyNames))
			warnOnce("invalid:"+raw, w)
			return env, SrcEnvInvalid, w
		}
		warnOnce("env:"+raw, fmt.Sprintf("%s=%s is set: it applies to every mission that does not choose its own failure_policy", EnvDefaultFailurePolicy, env))
		if tpl != "" && tpl != env {
			w := fmt.Sprintf("%s=%s overrides the template default %s of '%s'", EnvDefaultFailurePolicy, env, tpl, template)
			warnOnce("override:"+raw+":"+template, w)
			return env, SrcEnv, w
		}
		return env, SrcEnv, ""
	}
	if tpl != "" {
		return tpl, SrcTemplate, ""
	}
	return PolicyHaltAndReport, SrcDefault, ""
}

// ResolveDefault is the non-forked default for a template. Resolved ONCE at mission creation and frozen into
// MissionCreated (default_failure_policy + default_policy_source/_warning) so a later env/template change cannot
// alter how an old mission replays.
func ResolveDefault(template string) string {
	p, _, _ := ResolveDefaultDetailed(template)
	return p
}

// IsLegacyFrozen: a mission created before default_policy_source existed (<= 0.3.32) whose frozen default is
// COMPENSATE_DECLARED for a template whose default is NOW PRESERVE_ONLY. It keeps auto-reversing (the frozen
// value is authoritative for replay) unless the operator sets AGENTIC_OS_RERESOLVE_LEGACY_POLICY=1.
func IsLegacyFrozen(m *Mission) bool {
	return m.FailurePolicy == "" && m.DefaultPolicySource == "" &&
		parsePolicy(m.DefaultFailurePolicy) == PolicyCompensateDeclared &&
		parsePolicy(TemplateFailurePolicy[m.Template]) == PolicyPreserveOnly
}

// EffectivePolicy: the mission's own failure_policy (explicit opt-in), then the default FROZEN at creation, and only
// for a legacy mission without a frozen default the live env/template resolution. Fork branches ignore the default
// and use HALT_AND_REPORT unless the mission itself opts in. Mirrors compensation_policy.effective_policy.
func EffectivePolicy(m *Mission, forked bool) string {
	if p := parsePolicy(m.FailurePolicy); p != "" {
		return p
	}
	if forked {
		return PolicyHaltAndReport
	}
	if p := parsePolicy(m.DefaultFailurePolicy); p != "" {
		return p
	}
	return ResolveDefault(m.Template)
}

// EffectiveFailurePolicy is EffectivePolicy for a sequential (non-forked) mission.
func EffectiveFailurePolicy(m *Mission) string { return EffectivePolicy(m, false) }
