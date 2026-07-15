package mission

import "testing"

// ─── belief.Fuse ─────────────────────────────────────────────────────────────

func TestFuseSingleUncertainSourceIsLowConfidence(t *testing.T) {
	// one source, confidence 0.5 → agreement 1.0 × reliability 0.5 = 0.5 (not 1.0)
	b := Fuse([]map[string]any{{"value": "pro", "source": "crm", "confidence": 0.5}})
	if b.Value != "pro" || b.Confidence != 0.5 || b.Conflict {
		t.Fatalf("single-uncertain = %+v", b)
	}
}

func TestFuseConflictWhenSourcesDisagree(t *testing.T) {
	b := Fuse([]map[string]any{
		{"value": "pro", "source": "crm", "confidence": 1.0},
		{"value": "enterprise", "source": "inbox", "confidence": 1.0},
	})
	if !b.Conflict {
		t.Fatalf("high-confidence disagreement should flag conflict: %+v", b)
	}
}

func TestFuseStrongestMassWins(t *testing.T) {
	b := Fuse([]map[string]any{
		{"value": "a", "source": "s1", "confidence": 0.2},
		{"value": "b", "source": "s2", "confidence": 0.9},
		{"value": "b", "source": "s3", "confidence": 0.9},
	})
	if b.Value != "b" || len(b.Sources) != 2 {
		t.Fatalf("mass winner = %+v", b)
	}
}

// ─── cost ────────────────────────────────────────────────────────────────────

func TestCapabilityScoreAndRank(t *testing.T) {
	hi := NewCapabilitySpec("a", "op")
	hi.EstimatedValue = "high"
	lo := NewCapabilitySpec("b", "op")
	lo.EstimatedValue = "low"
	ranked := rankCandidates([]*CapabilitySpec{&lo, &hi})
	if ranked[0].Name != "a" {
		t.Fatalf("high-value should rank first: %v", ranked[0].Name)
	}
	// side-effecting + low confidence is penalised
	risky := NewCapabilitySpec("r", "op")
	risky.SideEffecting = true
	risky.Confidence = 0.1
	if capabilityScore(&risky) >= capabilityScore(&hi) {
		t.Fatal("risky low-confidence write should score below a clean high-value cap")
	}
}

// ─── registry + policy scoping ───────────────────────────────────────────────

func fleet() *CapabilityRegistry {
	r := NewCapabilityRegistry(nil)
	crm := NewCapabilitySpec("crm.find_contacts", "crm")
	crm.Provides = []string{"contacts"}
	crm.Permissions = []string{"crm:read"}
	bill := NewCapabilitySpec("billing.charge", "billing")
	bill.Provides = []string{"charged"}
	bill.SideEffecting = true
	bill.Permissions = []string{"billing:write"}
	r.Register(CapabilityManifest{Operator: "crm", Capabilities: []CapabilitySpec{crm}})
	r.Register(CapabilityManifest{Operator: "billing", Capabilities: []CapabilitySpec{bill}})
	return r
}

func TestRegistryProvidingAndDiscover(t *testing.T) {
	r := fleet()
	if got := r.Providing("charged"); len(got) != 1 || got[0].Name != "billing.charge" {
		t.Fatalf("Providing(charged) = %v", got)
	}
	// discovery ranks the contacts capability above billing for a "find people" need
	top := r.Discover("find people who fit this profile", 5)
	if len(top) == 0 || top[0].Cap.Name != "crm.find_contacts" {
		t.Fatalf("Discover top = %+v", top)
	}
	// registration set embeddings
	if len(r.Get("crm.find_contacts").Embedding) != 256 {
		t.Fatal("embedding not set on register")
	}
}

func TestPolicyScopedRegistryHidesForbidden(t *testing.T) {
	scoped := NewPolicyScopedRegistry(fleet(), []string{"crm:read"}) // no billing:write
	if scoped.Get("billing.charge") != nil {
		t.Fatal("forbidden capability must not be gettable")
	}
	if len(scoped.Providing("charged")) != 0 {
		t.Fatal("forbidden capability must not be a candidate")
	}
	if scoped.Get("crm.find_contacts") == nil {
		t.Fatal("permitted capability should be visible")
	}
	// star grant sees everything
	all := NewPolicyScopedRegistry(fleet(), []string{"*"})
	if len(all.All()) != 2 {
		t.Fatalf("star grant should see all: %d", len(all.All()))
	}
}
