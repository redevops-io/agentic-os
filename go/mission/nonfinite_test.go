package mission

import (
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// NaN/Inf in an event payload used to make json.Marshal fail and the event vanished from the persisted log.
func TestNonFiniteEventsSurviveReload(t *testing.T) {
	path := filepath.Join(t.TempDir(), "log.jsonl")
	s := NewEventStore(path)
	s.Append("ObservationWritten", "m", map[string]any{"key": "a", "value": math.NaN(),
		"nested": map[string]any{"hi": math.Inf(1), "lo": math.Inf(-1), "xs": []any{1.5, math.NaN()}}})
	s.Append("After", "m", map[string]any{"ok": true})
	if s.PersistFailures() != 0 {
		t.Fatalf("persist failures = %d", s.PersistFailures())
	}
	raw, _ := os.ReadFile(path)
	if len(raw) == 0 || !strings.Contains(string(raw), "NaN") || !strings.Contains(string(raw), "-Infinity") {
		t.Fatalf("expected Python-compatible tokens in the log: %q", raw)
	}
	r := NewEventStore(path)
	evs := r.ForMission("m")
	if len(evs) != 2 || r.LoadSkipped() != 0 {
		t.Fatalf("reload lost events: %d skipped=%d", len(evs), r.LoadSkipped())
	}
	p := evs[0].Payload
	if v, _ := p["value"].(float64); !math.IsNaN(v) {
		t.Fatalf("NaN not restored: %v", p["value"])
	}
	n := p["nested"].(map[string]any)
	if !math.IsInf(n["hi"].(float64), 1) || !math.IsInf(n["lo"].(float64), -1) || !math.IsNaN(n["xs"].([]any)[1].(float64)) {
		t.Fatalf("nested non-finite not restored: %v", n)
	}
}

// A log written by Python (bare NaN tokens) must load too.
func TestPythonWrittenNaNLogLoads(t *testing.T) {
	path := filepath.Join(t.TempDir(), "py.jsonl")
	line := `{"type": "E", "mission_id": "m", "payload": {"s": "NaN inside a string", "v": NaN, "w": -Infinity}, "seq": 1, "ts": 1.0}` + "\n"
	_ = os.WriteFile(path, []byte(line), 0o644)
	r := NewEventStore(path)
	evs := r.ForMission("m")
	if len(evs) != 1 || evs[0].Payload["s"] != "NaN inside a string" || !math.IsNaN(evs[0].Payload["v"].(float64)) {
		t.Fatalf("python NaN log not loaded: %+v skipped=%d", evs, r.LoadSkipped())
	}
}

func TestUnencodableEventIsCountedNotSilent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "bad.jsonl")
	s := NewEventStore(path)
	s.Append("Bad", "m", map[string]any{"c": make(chan int)})
	if s.PersistFailures() != 1 {
		t.Fatalf("an unwritable event must be counted, got %d", s.PersistFailures())
	}
}

// Every container shape must survive: the sanitiser is generic, not a type switch over a few shapes.
func TestNonFiniteEventsSurviveForEveryPayloadShape(t *testing.T) {
	type rec struct {
		X    float64  `json:"x"`
		P    *float64 `json:"p"`
		Skip float64  `json:"-"`
	}
	nan, inf := math.NaN(), math.Inf(-1)
	path := filepath.Join(t.TempDir(), "shapes.jsonl")
	s := NewEventStore(path)
	s.Append("Shapes", "m", map[string]any{
		"maps":   []map[string]any{{"v": nan}, {"v": 2.5}},
		"typed":  map[string]float64{"a": inf, "b": 1},
		"struct": rec{X: nan, P: &inf},
		"ptr":    &nan,
		"floats": []float64{1, nan},
		"arr":    [2]float64{inf, 0},
		"strs":   []string{"ok"},
	})
	if s.PersistFailures() != 0 {
		t.Fatalf("persist failures: %d", s.PersistFailures())
	}
	r := NewEventStore(path)
	evs := r.ForMission("m")
	if len(evs) != 1 || r.LoadSkipped() != 0 {
		t.Fatalf("event lost: %d skipped=%d", len(evs), r.LoadSkipped())
	}
	p := evs[0].Payload
	if !math.IsNaN(p["maps"].([]any)[0].(map[string]any)["v"].(float64)) ||
		!math.IsInf(p["typed"].(map[string]any)["a"].(float64), -1) ||
		!math.IsNaN(p["struct"].(map[string]any)["x"].(float64)) ||
		!math.IsInf(p["struct"].(map[string]any)["p"].(float64), -1) ||
		!math.IsNaN(p["ptr"].(float64)) || !math.IsNaN(p["floats"].([]any)[1].(float64)) ||
		!math.IsInf(p["arr"].([]any)[0].(float64), -1) {
		t.Fatalf("non-finite values not restored: %v", p)
	}
}
