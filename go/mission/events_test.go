package mission

import "testing"

// The golden event_ids + NDJSON below were produced by the Python source of truth
// (agentic_os/mission/events.py). Asserting them here proves the Go port is BIT-IDENTICAL — the
// content-addressed id key and the NDJSON encoding agree with Python (and Kotlin), not merely that
// Go is self-consistent.

func TestRuntimeEventContentAddressedIDMatchesPython(t *testing.T) {
	cases := []struct {
		name string
		got  string
		want string
	}{
		{"capability", CapabilityEvent("agent", 1, "cap.read", RuntimeEvent{MissionID: "m1"}).EventID, "ev-f8c203e1a99bd902"},
		{"dereference", DereferenceEvent("agent", 3, []string{"art-1"}, "", RuntimeEvent{}).EventID, "ev-ac168ae2b0338f2d"},
		{"capability_plan", CapabilityEvent("agent", 4, "cap.plan", RuntimeEvent{SessionID: "s1"}).EventID, "ev-e52bbaeaa00821b9"},
		{"evidence_change", EvidenceChangeEvent("agent", 6, "art-9", "updated", RuntimeEvent{}).EventID, "ev-d9ced1c9bad5de9b"},
	}
	for _, c := range cases {
		if c.got != c.want {
			t.Errorf("%s event_id: got %s want %s (Python golden)", c.name, c.got, c.want)
		}
	}

	// deterministic: same inputs → same id (replayable)
	a := CapabilityEvent("agent", 1, "cap.read", RuntimeEvent{MissionID: "m1"})
	b := CapabilityEvent("agent", 1, "cap.read", RuntimeEvent{MissionID: "m1"})
	if a.EventID != b.EventID {
		t.Errorf("content-addressed id not deterministic: %s != %s", a.EventID, b.EventID)
	}

	// the causal child records its parent, and its id matches Python
	p := CapabilityEvent("agent", 4, "cap.plan", RuntimeEvent{SessionID: "s1"})
	child := ToolResultEvent("agent", 5, "cap.plan", RuntimeEvent{SessionID: "s1", ParentEventID: p.EventID})
	if child.EventID != "ev-461c3326f317a6c9" {
		t.Errorf("child event_id: got %s want ev-461c3326f317a6c9", child.EventID)
	}
}

func TestRuntimeEventNDJSONMatchesPython(t *testing.T) {
	e1 := CapabilityEvent("agent", 1, "cap.read", RuntimeEvent{MissionID: "m1"})
	wantE1 := `{"actor": "agent", "artifact_handles": [], "capability_id": "cap.read", "event_id": "ev-f8c203e1a99bd902", "event_type": "capability_invocation", "evidence_refs": [], "mission_id": "m1", "parent_event_id": "", "payload": {}, "policy_context": "", "result_status": "attempted", "schema_version": "runtime-event/v10", "session_id": "", "source_agent": "", "source_runtime": "", "timestamp": 1, "visibility": "private"}`
	if got := e1.ToNDJSON(); got != wantE1 {
		t.Errorf("e1 NDJSON not byte-identical to Python:\n got  %s\n want %s", got, wantE1)
	}

	evc := EvidenceChangeEvent("agent", 6, "art-9", "updated", RuntimeEvent{})
	wantEvc := `{"actor": "agent", "artifact_handles": [], "capability_id": "", "event_id": "ev-d9ced1c9bad5de9b", "event_type": "evidence_change", "evidence_refs": ["art-9"], "mission_id": "", "parent_event_id": "", "payload": {"change_type": "updated", "ref": "art-9"}, "policy_context": "", "result_status": "observed", "schema_version": "runtime-event/v10", "session_id": "", "source_agent": "", "source_runtime": "", "timestamp": 6, "visibility": "private"}`
	if got := evc.ToNDJSON(); got != wantEvc {
		t.Errorf("evidence NDJSON not byte-identical to Python:\n got  %s\n want %s", got, wantEvc)
	}

	// round-trips byte-stably (id preserved, not recomputed)
	back, err := RuntimeEventFromNDJSON(e1.ToNDJSON())
	if err != nil {
		t.Fatalf("from ndjson: %v", err)
	}
	if back.ToNDJSON() != e1.ToNDJSON() {
		t.Errorf("NDJSON round-trip not stable:\n first  %s\n second %s", e1.ToNDJSON(), back.ToNDJSON())
	}
}

func TestRuntimeEventValidate(t *testing.T) {
	// a dereference without artifact_handles is missing a specialized required field
	bad := NewRuntimeEvent(RuntimeEvent{EventType: EvDereference, Actor: "agent", Timestamp: 2})
	missing := bad.Validate()
	found := false
	for _, f := range missing {
		if f == "artifact_handles" {
			found = true
		}
	}
	if !found {
		t.Errorf("validate should flag missing artifact_handles, got %v", missing)
	}

	good := DereferenceEvent("agent", 3, []string{"art-1"}, "", RuntimeEvent{})
	if v := good.Validate(); len(v) != 0 {
		t.Errorf("a complete event should validate clean, got %v", v)
	}
}

func TestEventLedger(t *testing.T) {
	led := NewEventLedger()
	p := led.Append(CapabilityEvent("agent", 4, "cap.plan", RuntimeEvent{SessionID: "s1"}))
	led.Append(ToolResultEvent("agent", 5, "cap.plan", RuntimeEvent{SessionID: "s1", ParentEventID: p.EventID}))

	// replays from NDJSON identically (physical order + causal edges preserved)
	replay, err := EventLedgerFromNDJSON(led.ToNDJSON())
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	if replay.ToNDJSON() != led.ToNDJSON() {
		t.Error("ledger did not replay from NDJSON identically")
	}
	if got := led.Children(p.EventID); len(got) != 1 {
		t.Errorf("causal children: got %d want 1", len(got))
	}
	if got, ok := led.Get(p.EventID); !ok || got.CapabilityID != "cap.plan" {
		t.Errorf("Get(%s) = %v,%v", p.EventID, got, ok)
	}

	sum := led.SessionSummary("s1")
	if sum["event_count"] != 2 {
		t.Errorf("session summary event_count: got %v want 2", sum["event_count"])
	}
	types, _ := sum["types"].([]any)
	if len(types) != 2 {
		t.Errorf("session summary types: got %v", sum["types"])
	}
}
