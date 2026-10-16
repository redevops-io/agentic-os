package mission

import (
	"fmt"
	"sync"
	"testing"
	"time"
)

// Many goroutines invoking the in-memory client and the operator at once; run under -race.
func TestConcurrentInvokeIsSynchronised(t *testing.T) {
	h := map[string]Handler{"c": func(map[string]any) (map[string]any, error) { return map[string]any{"ok": true}, nil }}
	client := NewInMemoryOperatorClient(h)
	op := NewOperator("op", []Capability{NewCapability("c", h["c"])})
	var wg sync.WaitGroup
	for g := 0; g < 64; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 50; i++ {
				key := fmt.Sprintf("k%d", (g*50+i)%40)
				if _, err := client.Invoke("op", "c", nil, key); err != nil {
					t.Error(err)
				}
				if _, err := op.Invoke("c", nil, key); err != nil {
					t.Error(err)
				}
				_ = client.CallsSnapshot()
				_ = op.CallsSnapshot()
			}
		}(g)
	}
	wg.Wait()
	if len(client.CallsSnapshot()) == 0 || len(op.CallsSnapshot()) == 0 {
		t.Fatal("no calls recorded")
	}
}

// Whole mission waves with many parallel nodes, repeated.
func TestParallelWavesRaceFree(t *testing.T) {
	for r := 0; r < 5; r++ {
		var peak int32
		rt, mid := buildWaveMission(32, 32, time.Millisecond, &peak)
		rt.Run(mid)
		if st := rt.missions[mid].State; st != MissionSucceeded {
			t.Fatalf("run %d: state %v", r, st)
		}
	}
}
