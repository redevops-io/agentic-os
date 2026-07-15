package mission

import "testing"

// a trivial fuse for world-state tests: last observation wins, its confidence carried through.
func lastWins(obs []map[string]any) Belief {
	o := obs[len(obs)-1]
	conf, _ := o["confidence"].(float64)
	src, _ := o["source"].(string)
	return Belief{Value: o["value"], Confidence: conf, Sources: []string{src}}
}

func TestEventStoreAppendAndFold(t *testing.T) {
	s := NewEventStore("")
	s.Append("MissionCreated", "m1", map[string]any{"goal": "ship", "template": "onboarding"})
	s.Append("MissionStateChanged", "m1", map[string]any{"state": "running"})
	s.Append("MissionCreated", "m2", map[string]any{"goal": "other"})

	if got := s.ForMission("m1"); len(got) != 2 {
		t.Fatalf("ForMission m1 = %d events", len(got))
	}
	if ids := s.MissionIDs(); len(ids) != 2 || ids[0] != "m1" {
		t.Fatalf("MissionIDs = %v", ids)
	}

	repo := NewMissionRepository(s)
	if st, ok := repo.State("m1"); !ok || st != MissionRunning {
		t.Fatalf("State m1 = %v,%v", st, ok)
	}
	if repo.Goal("m1") != "ship" {
		t.Fatalf("Goal m1 = %q", repo.Goal("m1"))
	}
	if len(repo.ListMissions()) != 2 {
		t.Fatal("ListMissions should see both missions")
	}
}

func TestNodeStatusAndPendingHumanFold(t *testing.T) {
	s := NewEventStore("")
	s.Append("NodeDispatched", "m", map[string]any{"node_id": "n1"})
	s.Append("NodeSucceeded", "m", map[string]any{"node_id": "n1", "result": map[string]any{"ok": true}})
	s.Append("NodeParked", "m", map[string]any{"node_id": "n2", "capability": "billing.charge",
		"human_task": map[string]any{"kind": "approval", "options": []any{"approve", "reject"}}})

	repo := NewMissionRepository(s)
	st := repo.NodeStatus("m")
	if st["n1"] != NodeDone || st["n2"] != NodeWaiting {
		t.Fatalf("NodeStatus = %v", st)
	}
	if r := repo.NodeResults("m"); r["n1"]["ok"] != true {
		t.Fatalf("NodeResults = %v", r)
	}
	pend := repo.PendingHuman("m")
	if pend == nil || pend["node_id"] != "n2" || pend["kind"] != "approval" {
		t.Fatalf("PendingHuman = %v", pend)
	}
	// resolving the park clears it
	s.Append("NodeResumed", "m", map[string]any{"node_id": "n2"})
	if repo.PendingHuman("m") != nil {
		t.Fatal("PendingHuman should be nil after resume")
	}
}

func TestWorldStateObserveAndSnapshot(t *testing.T) {
	s := NewEventStore("")
	w := NewWorldState("m", s, lastWins)
	if w.Get("plan") != nil {
		t.Fatal("unknown key should be nil")
	}
	w.Observe("plan", "pro", "billing", 1.0)
	w.Observe("plan", "enterprise", "human", 100.0)
	if w.Get("plan") != "enterprise" { // last-wins fuse
		t.Fatalf("Get plan = %v", w.Get("plan"))
	}
	b := w.Belief("plan")
	if b == nil || b.Confidence != 100.0 {
		t.Fatalf("Belief = %+v", b)
	}
	if snap := w.Snapshot(); len(snap) != 1 || snap["plan"].Value != "enterprise" {
		t.Fatalf("Snapshot = %v", snap)
	}
}

func TestSubscribeReceivesEvents(t *testing.T) {
	s := NewEventStore("")
	var got []string
	s.Subscribe(func(e *Event) { got = append(got, e.Type) })
	s.Append("A", "m", map[string]any{})
	s.Append("B", "m", map[string]any{})
	if len(got) != 2 || got[0] != "A" || got[1] != "B" {
		t.Fatalf("subscriber saw %v", got)
	}
}
