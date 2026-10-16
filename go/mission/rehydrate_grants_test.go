package mission

import (
	"errors"
	"testing"
)

// Restart must not widen authority: the grants recorded at creation survive rehydration, and a log that
// recorded none restores with NO grants (or an explicit error), never "*".
func TestRehydrateKeepsRecordedGrants(t *testing.T) {
	rt, reg := onboardingRuntime(t)
	m := rt.CreateMission("onboard", CreateOpts{PolicyRefs: []string{"billing.write"}, Template: "onboarding"})
	rt2 := NewMissionRuntime(reg, NewExecutor(NewInMemoryOperatorClient(nil)), WithStore(rt.store))
	got := rt2.Rehydrate(m.ID)
	if len(got.PolicyRefs) != 1 || got.PolicyRefs[0] != "billing.write" {
		t.Fatalf("rehydrated PolicyRefs = %v, want recorded [billing.write]", got.PolicyRefs)
	}
}

func TestRehydrateLegacyLogNeverFabricatesWildcard(t *testing.T) {
	_, reg := onboardingRuntime(t)
	store := NewEventStore("")
	store.Append("MissionCreated", "legacy", map[string]any{"goal": "g", "template": "onboarding"}) // no policy_refs
	rt := NewMissionRuntime(reg, NewExecutor(NewInMemoryOperatorClient(nil)), WithStore(store))
	if _, err := rt.RehydrateStrict("legacy", RehydrateOpts{}); !errors.Is(err, ErrUnrecoverableAuthority) {
		t.Fatalf("strict rehydrate of an unrecorded log must refuse, got %v", err)
	}
	if got := rt.Rehydrate("legacy").PolicyRefs; len(got) != 0 {
		t.Fatalf("lenient rehydrate = %v, want no grants", got)
	}
	m, err := rt.RehydrateStrict("legacy", RehydrateOpts{PolicyRefs: []string{"srv.grant"}})
	if err != nil || len(m.PolicyRefs) != 1 || m.PolicyRefs[0] != "srv.grant" {
		t.Fatalf("explicit grants must be honoured: %v %v", m, err)
	}
}

func TestRehydrateRestrictivePolicyStaysRestrictive(t *testing.T) {
	rt, reg := onboardingRuntime(t)
	m := rt.CreateMission("onboard", CreateOpts{PolicyRefs: []string{}, Template: "onboarding"})
	rt2 := NewMissionRuntime(reg, NewExecutor(NewInMemoryOperatorClient(nil)), WithStore(rt.store))
	got := rt2.Rehydrate(m.ID)
	for _, g := range got.PolicyRefs {
		if g == "*" {
			t.Fatal("a mission created with no grants was rehydrated as wildcard")
		}
	}
}
