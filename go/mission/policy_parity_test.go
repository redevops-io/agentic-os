package mission

import (
	"encoding/json"
	"os"
	"testing"
)

// Shared Python<->Go fixtures: testdata/policy_resolution.json is produced by scripts/gen_parity_fixtures.py from the
// Python implementation (tests/test_go_parity_fixtures.py regenerates and compares it). Go must agree on every case.
type policyFixture struct {
	Resolve []struct {
		Template *string `json:"template"`
		Env      *string `json:"env"`
		Policy   string  `json:"policy"`
		Source   string  `json:"source"`
		Warning  string  `json:"warning"`
	} `json:"resolve"`
	Effective []struct {
		Template             *string `json:"template"`
		FailurePolicy        string  `json:"failure_policy"`
		DefaultFailurePolicy string  `json:"default_failure_policy"`
		Forked               bool    `json:"forked"`
		Env                  *string `json:"env"`
		Policy               string  `json:"policy"`
	} `json:"effective"`
	Legacy []struct {
		Template             *string `json:"template"`
		FailurePolicy        string  `json:"failure_policy"`
		DefaultPolicySource  string  `json:"default_policy_source"`
		DefaultFailurePolicy string  `json:"default_failure_policy"`
		Legacy               bool    `json:"legacy"`
	} `json:"legacy"`
	Created []struct {
		Template        *string `json:"template"`
		FailurePolicy   string  `json:"failure_policy"`
		Env             *string `json:"env"`
		Recorded        string  `json:"recorded_failure_policy"`
		DefaultPolicy   string  `json:"default_failure_policy"`
		DefaultSource   string  `json:"default_policy_source"`
		DefaultWarning  string  `json:"default_policy_warning"`
		EffectivePolicy string  `json:"effective"`
	} `json:"created"`
}

func loadPolicyFixture(t *testing.T) *policyFixture {
	t.Helper()
	raw, err := os.ReadFile("testdata/policy_resolution.json")
	if err != nil {
		t.Fatal(err)
	}
	var f policyFixture
	if err := json.Unmarshal(raw, &f); err != nil {
		t.Fatal(err)
	}
	return &f
}

func sv(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func setEnv(t *testing.T, v *string) {
	t.Helper()
	if v == nil {
		os.Unsetenv(EnvDefaultFailurePolicy)
		t.Cleanup(func() { os.Unsetenv(EnvDefaultFailurePolicy) })
		return
	}
	t.Setenv(EnvDefaultFailurePolicy, *v)
}

func TestPolicyResolutionMatchesPython(t *testing.T) {
	f := loadPolicyFixture(t)
	if len(f.Resolve) == 0 {
		t.Fatal("empty fixture")
	}
	for _, c := range f.Resolve {
		setEnv(t, c.Env)
		p, src, w := ResolveDefaultDetailed(sv(c.Template))
		if p != c.Policy || src != c.Source || w != c.Warning {
			t.Errorf("resolve(%v, env=%v): go=(%s,%s,%q) python=(%s,%s,%q)", sv(c.Template), sv(c.Env), p, src, w, c.Policy, c.Source, c.Warning)
		}
	}
}

func TestEffectivePolicyMatchesPython(t *testing.T) {
	for _, c := range loadPolicyFixture(t).Effective {
		setEnv(t, c.Env)
		m := &Mission{Template: sv(c.Template), FailurePolicy: c.FailurePolicy, DefaultFailurePolicy: c.DefaultFailurePolicy}
		if got := EffectivePolicy(m, c.Forked); got != c.Policy {
			t.Errorf("effective(%+v env=%v): go=%s python=%s", c, sv(c.Env), got, c.Policy)
		}
	}
}

func TestLegacyFrozenMatchesPython(t *testing.T) {
	for _, c := range loadPolicyFixture(t).Legacy {
		m := &Mission{Template: sv(c.Template), FailurePolicy: c.FailurePolicy, DefaultPolicySource: c.DefaultPolicySource,
			DefaultFailurePolicy: c.DefaultFailurePolicy}
		if got := IsLegacyFrozen(m); got != c.Legacy {
			t.Errorf("legacy(%+v): go=%v python=%v", c, got, c.Legacy)
		}
	}
}

// The default is resolved ONCE at creation, recorded in MissionCreated, and replay never re-reads the env.
func TestMissionCreatedFreezesDefaultLikePython(t *testing.T) {
	for _, c := range loadPolicyFixture(t).Created {
		setEnv(t, c.Env)
		rt := NewMissionRuntime(NewCapabilityRegistry(nil), NewExecutor(NewInMemoryOperatorClient(nil)))
		m := rt.CreateMission("g", CreateOpts{PolicyRefs: []string{"*"}, Template: sv(c.Template), FailurePolicy: c.FailurePolicy})
		var p map[string]any
		for _, e := range rt.store.ForMission(m.ID) {
			if e.Type == "MissionCreated" {
				p = e.Payload
			}
		}
		str := func(k string) string { s, _ := p[k].(string); return s }
		if str("failure_policy") != c.Recorded || str("default_failure_policy") != c.DefaultPolicy ||
			str("default_policy_source") != c.DefaultSource || str("default_policy_warning") != c.DefaultWarning {
			t.Errorf("MissionCreated(%+v env=%v): go=%v", c, sv(c.Env), p)
		}
		if got := EffectiveFailurePolicy(m); got != c.EffectivePolicy {
			t.Errorf("effective after create (%+v): go=%s python=%s", c, got, c.EffectivePolicy)
		}
		// replay: changing the env afterwards must not change what the old mission does
		t.Setenv(EnvDefaultFailurePolicy, "COMPENSATE_DECLARED")
		rt2 := NewMissionRuntime(NewCapabilityRegistry(nil), NewExecutor(NewInMemoryOperatorClient(nil)), WithStore(rt.store))
		got, err := rt2.RehydrateStrict(m.ID, RehydrateOpts{})
		if err != nil {
			t.Fatal(err)
		}
		if EffectiveFailurePolicy(got) != c.EffectivePolicy {
			t.Errorf("rehydrated policy drifted with the env (%+v): %s want %s", c, EffectiveFailurePolicy(got), c.EffectivePolicy)
		}
	}
}

func TestRecordedReresolutionIsReappliedOnRehydrate(t *testing.T) {
	t.Setenv(EnvDefaultFailurePolicy, "")
	store := NewEventStore("")
	store.Append("MissionCreated", "old", map[string]any{"goal": "g", "template": "onboarding", "policy_refs": []any{"*"},
		"default_failure_policy": "COMPENSATE_DECLARED"}) // pre-0.3.33 log: no default_policy_source
	rt := NewMissionRuntime(NewCapabilityRegistry(nil), NewExecutor(NewInMemoryOperatorClient(nil)), WithStore(store))
	m, _ := rt.RehydrateStrict("old", RehydrateOpts{})
	if !IsLegacyFrozen(m) || EffectiveFailurePolicy(m) != PolicyCompensateDeclared {
		t.Fatalf("a legacy mission keeps its frozen COMPENSATE_DECLARED: %+v", m)
	}
	store.Append("DefaultPolicyReresolved", "old", map[string]any{"template": "onboarding", "from": "COMPENSATE_DECLARED", "to": "PRESERVE_ONLY"})
	m, _ = rt.RehydrateStrict("old", RehydrateOpts{})
	if EffectiveFailurePolicy(m) != PolicyPreserveOnly || m.DefaultPolicySource != "reresolved" || IsLegacyFrozen(m) {
		t.Fatalf("recorded re-resolution not re-applied: %+v", m)
	}
}
