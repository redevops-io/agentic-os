package mission

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

type goldenCase struct {
	Name     string           `json:"name"`
	ForkID   string           `json:"fork_id"`
	Events   []map[string]any `json:"events"`
	Expected map[string]any   `json:"expected"`
}

func normalize(t *testing.T, v any) any {
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

// The golden file is produced by Python (tests/test_mission_fork_join.py regenerates and compares it), so
// Python and Go provably fold the same event log to the same state.
func TestForkFoldMatchesPythonGolden(t *testing.T) {
	raw, err := os.ReadFile("testdata/fork_join_golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []goldenCase
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases) == 0 {
		t.Fatal("no golden cases")
	}
	for _, c := range cases {
		var evs []*Event
		for i, e := range c.Events {
			p, _ := e["payload"].(map[string]any)
			evs = append(evs, &Event{Type: e["type"].(string), MissionID: "m", Payload: p, Seq: i + 1})
		}
		got := normalize(t, FoldFork(evs, c.ForkID).Dict())
		want := normalize(t, c.Expected)
		if !reflect.DeepEqual(got, want) {
			t.Errorf("%s: fold mismatch\n got: %v\nwant: %v", c.Name, got, want)
		}
		// replay equality: folding twice gives the same state
		if !reflect.DeepEqual(normalize(t, FoldFork(evs, c.ForkID).Dict()), got) {
			t.Errorf("%s: fold not deterministic", c.Name)
		}
		if ids := ForkIDs(evs); len(ids) != 1 || ids[0] != c.ForkID {
			t.Errorf("%s: ForkIDs=%v", c.Name, ids)
		}
	}
}

func TestEvaluateJoinMatrix(t *testing.T) {
	cases := []struct {
		mode       string
		k, n, s, f int
		c          int
		want       string
	}{
		{"all", 0, 3, 3, 0, 0, "satisfied"}, {"all", 0, 3, 1, 1, 0, "failed"}, {"all", 0, 3, 1, 0, 0, "pending"},
		{"any", 0, 3, 0, 2, 0, "pending"}, {"any", 0, 3, 0, 3, 0, "failed"}, {"any", 0, 3, 1, 2, 0, "satisfied"},
		{"quorum", 2, 3, 2, 1, 0, "satisfied"}, {"quorum", 2, 3, 1, 2, 0, "failed"}, {"quorum", 2, 3, 1, 1, 0, "pending"},
	}
	for _, c := range cases {
		if got := EvaluateJoin(c.mode, c.k, c.n, c.s, c.f, c.c); got != c.want {
			t.Errorf("%+v got %s", c, got)
		}
	}
}

// Existing mission folds must ignore fork events without error (replay never chokes on a forked log).
func TestExistingFoldsIgnoreForkEvents(t *testing.T) {
	st := NewEventStore("")
	st.Append("MissionCreated", "m1", map[string]any{"goal": "g"})
	st.Append("ForkOpened", "m1", map[string]any{"fork_id": "f", "branches": []any{}})
	st.Append("BranchStarted", "m1", map[string]any{"fork_id": "f", "branch_id": "b"})
	repo := NewMissionRepository(st)
	if len(repo.NodeStatus("m1")) != 0 || repo.PendingHuman("m1") != nil {
		t.Fatal("fork events leaked into node folds")
	}
	if got, _ := repo.State("m1"); got != MissionPlanning {
		t.Fatalf("state=%v", got)
	}
}
