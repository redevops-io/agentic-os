package mission

import (
	"encoding/json"
	"testing"
)

// Kernel-contract parity with Python (2026-10): Node.resource + Mission.principal/tenant + an in-memory store.
func TestNodeResourceField(t *testing.T) {
	n := Node{Capability: "c", Operator: "o", Resource: "stripe:acme"}
	b, _ := json.Marshal(n)
	var back map[string]any
	_ = json.Unmarshal(b, &back)
	if back["resource"] != "stripe:acme" {
		t.Fatalf("Node.resource not serialized: %v", back["resource"])
	}
}

func TestMissionPrincipalTenantFields(t *testing.T) {
	m := Mission{Goal: "g", Principal: "user:dana", Tenant: "acme"}
	b, _ := json.Marshal(m)
	var back map[string]any
	_ = json.Unmarshal(b, &back)
	if back["principal"] != "user:dana" || back["tenant"] != "acme" {
		t.Fatalf("Mission principal/tenant not serialized: %v / %v", back["principal"], back["tenant"])
	}
}

func TestMemoryEventStoreFoldParity(t *testing.T) {
	var s EventStoreIface = NewMemoryEventStore()
	s.Append("ObservationWritten", "m1", map[string]any{"k": "v"})
	s.Append("ObservationWritten", "m2", map[string]any{"k": "w"})
	if len(s.All()) != 2 {
		t.Fatalf("want 2 events, got %d", len(s.All()))
	}
	if len(s.ForMission("m1")) != 1 {
		t.Fatalf("want 1 event for m1")
	}
	ids := s.MissionIDs()
	if len(ids) != 2 || ids[0] != "m1" || ids[1] != "m2" {
		t.Fatalf("mission ids wrong: %v", ids)
	}
}
