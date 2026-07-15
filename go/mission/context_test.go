package mission

// Context Runtime — the single resolution door (Go port of tests/test_context.py). Every context
// decision the Mission Runtime makes goes through Resolve(), and each answer carries provenance.

import "testing"

func ctxRegistry() *CapabilityRegistry {
	reg := NewCapabilityRegistry(nil)
	a := CapabilitySpec{Name: "crm.find_contacts", Operator: "crm", Provides: []string{"contacts"}}
	b := CapabilitySpec{Name: "billing.charge", Operator: "billing", Provides: []string{"charge_done"}}
	reg.Register(CapabilityManifest{Operator: "crm", Capabilities: []CapabilitySpec{a}})
	reg.Register(CapabilityManifest{Operator: "billing", Capabilities: []CapabilitySpec{b}})
	return reg
}

func TestResolveBindsByExactProvidesWithProvenance(t *testing.T) {
	cr := NewLocalContextRuntime(ctxRegistry())
	b := cr.Resolve(ContextIntent{Kind: BindCapability, Outcome: "contacts", Need: "find people"})
	cap, ok := b.Value.(*CapabilitySpec)
	if !ok || cap.Name != "crm.find_contacts" {
		t.Fatalf("expected crm.find_contacts, got %v", b.Value)
	}
	if b.Provenance.Representation != "provides" || b.Provenance.Resolver != "capability-registry" {
		t.Fatalf("bad provenance: %+v", b.Provenance)
	}
}

func TestResolveHonoursPrefer(t *testing.T) {
	reg := ctxRegistry()
	c := CapabilitySpec{Name: "crm.import_contacts", Operator: "crm", Provides: []string{"contacts"}}
	reg.Register(CapabilityManifest{Operator: "crm", Capabilities: []CapabilitySpec{c}})
	cr := NewLocalContextRuntime(reg)
	b := cr.Resolve(ContextIntent{Kind: BindCapability, Outcome: "contacts", Need: "x", Prefer: "crm.import_contacts"})
	if cap, _ := b.Value.(*CapabilitySpec); cap == nil || cap.Name != "crm.import_contacts" {
		t.Fatalf("prefer not honoured: %v", b.Value)
	}
}

func TestResolveSelectModelReturnsConfiguredModel(t *testing.T) {
	cr := NewLocalContextRuntime(ctxRegistry())
	cr.Model = "qwen3.6-35b"
	b := cr.Resolve(ContextIntent{Kind: SelectModel, Goal: "plan"})
	if b.Value != "qwen3.6-35b" || b.Provenance.Representation != "model" {
		t.Fatalf("bad model resolution: %v %+v", b.Value, b.Provenance)
	}
}

func TestRouterPicksEngineByQueryShape(t *testing.T) {
	cases := []struct{ q, want string }{
		{"what happened before the outage", "graph"},       // temporal
		{"how does billing relate to churn", "hipporag"},   // multi-hop
		{"count invoices per customer", "analytical"},      // structured
		{"find the frame with the logo", "vision"},         // multimodal
		{"summarize the refund policy", "vector"},          // semantic default (NOT "sum")
	}
	for _, c := range cases {
		if got, _ := RouteRepresentation(c.q, ""); got != c.want {
			t.Errorf("route(%q) = %q, want %q", c.q, got, c.want)
		}
	}
	if got, _ := RouteRepresentation("anything", "graph"); got != "graph" { // caller pin
		t.Errorf("hint pin failed: %q", got)
	}
}

func TestRetrieveRoutesAndReturnsWithProvenance(t *testing.T) {
	corpus := []map[string]any{
		{"id": "d1", "text": "refund policy: 30-day window, no questions asked"},
		{"id": "d2", "text": "shipping and returns overview"},
	}
	cr := NewLocalContextRuntime(ctxRegistry())
	cr.Retrievers = map[string]KnowledgeRetriever{"vector": KeywordRetriever{Corpus: corpus}}
	b := cr.Resolve(ContextIntent{Kind: RetrieveKnowledge, Need: "what is the refund policy"})
	if b.Provenance.Representation != "vector" {
		t.Fatalf("expected vector representation, got %q", b.Provenance.Representation)
	}
	rows, _ := b.Value.([]map[string]any)
	if len(rows) == 0 || rows[0]["id"] != "d1" {
		t.Fatalf("refund doc did not rank first: %v", b.Value)
	}
	if !contains(b.Provenance.Alternatives, "graph") {
		t.Fatalf("other engines missing from alternatives: %v", b.Provenance.Alternatives)
	}
}

func TestRetrieveWithoutAWiredEngineExplainsTheRoute(t *testing.T) {
	cr := NewLocalContextRuntime(ctxRegistry()) // no retrievers wired
	b := cr.Resolve(ContextIntent{Kind: RetrieveKnowledge, Need: "how does A relate to B"})
	rows, _ := b.Value.([]map[string]any)
	if len(rows) != 0 {
		t.Fatalf("expected empty results, got %v", rows)
	}
	if b.Provenance.Representation != "hipporag" {
		t.Fatalf("expected hipporag route, got %q", b.Provenance.Representation)
	}
	if !contains2(b.Provenance.Reason, "no hipporag retriever wired") {
		t.Fatalf("route not explained: %q", b.Provenance.Reason)
	}
}

func contains(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}

func contains2(s, sub string) bool { return len(s) >= len(sub) && indexOf(s, sub) >= 0 }

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
