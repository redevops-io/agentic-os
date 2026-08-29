package mission

// Parallel wave EXECUTION (not just conflict-aware scheduling): independent ready nodes' operator
// invocations overlap, bounded, while the commit stays serial + deterministic. Parity with the Python
// _execute_wave and Kotlin thread-pool executors. Proves concurrent == serial (same committed state) AND
// that real overlap happens (faster + peak parallelism > 1).

import (
	"sync/atomic"
	"testing"
	"time"
)

// indepPlanner emits N independent (dependency-free) steps → one ready wave.
type indepPlanner struct{ outcomes []string }

func (p indepPlanner) Plan(missionID, goal string, ctx map[string]any) ExecutionIntent {
	steps := make([]IntentStep, len(p.outcomes))
	for i, o := range p.outcomes {
		steps[i] = NewIntentStep(o, "do "+o) // no InputsFrom → independent
	}
	return NewExecutionIntent(missionID, steps...)
}

func buildWaveMission(conc, n int, sleep time.Duration, peak *int32) (*MissionRuntime, string) {
	reg := NewCapabilityRegistry(nil)
	caps := make([]CapabilitySpec, n)
	handlers := map[string]Handler{}
	outcomes := make([]string, n)
	var cur int32
	for i := 0; i < n; i++ {
		o := "o" + itoa(i)
		outcomes[i] = o
		name := "op.c" + itoa(i)
		c := NewCapabilitySpec(name, "op")
		c.Provides = []string{o}
		caps[i] = c
		handlers[name] = func(inputs map[string]any) (map[string]any, error) {
			v := atomic.AddInt32(&cur, 1)
			for { // record the peak simultaneous handlers
				pk := atomic.LoadInt32(peak)
				if v <= pk || atomic.CompareAndSwapInt32(peak, pk, v) {
					break
				}
			}
			time.Sleep(sleep)
			atomic.AddInt32(&cur, -1)
			return map[string]any{"ok": true}, nil
		}
	}
	reg.Register(CapabilityManifest{Operator: "op", Capabilities: caps})
	rt := NewMissionRuntime(reg, NewExecutor(NewInMemoryOperatorClient(handlers)),
		WithPlanner(indepPlanner{outcomes}), WithMaxConcurrency(conc))
	m := rt.CreateMission("job", CreateOpts{PolicyRefs: []string{"*"}})
	return rt, m.ID
}

func succeededCount(rt *MissionRuntime, mid string) int {
	c := 0
	for _, e := range rt.repo.Timeline(mid) {
		if e["type"] == "NodeSucceeded" {
			c++
		}
	}
	return c
}

func TestConcurrentWaveEqualsSerialAndIsFaster(t *testing.T) {
	const n = 8
	const sleep = 15 * time.Millisecond

	var peakSerial, peakPar int32
	rtS, midS := buildWaveMission(1, n, sleep, &peakSerial)
	t0 := time.Now()
	rtS.Run(midS)
	serialWall := time.Since(t0)

	rtP, midP := buildWaveMission(n, n, sleep, &peakPar)
	t1 := time.Now()
	rtP.Run(midP)
	parWall := time.Since(t1)

	// equivalence: same terminal state, same nodes committed, same world size
	if st := rtS.missions[midS].State; st != MissionSucceeded {
		t.Fatalf("serial did not succeed: %v", st)
	}
	if st := rtP.missions[midP].State; st != MissionSucceeded {
		t.Fatalf("parallel did not succeed: %v", st)
	}
	if a, b := succeededCount(rtS, midS), succeededCount(rtP, midP); a != n || b != n {
		t.Fatalf("succeeded counts differ from n: serial=%d parallel=%d", a, b)
	}
	if a, b := len(rtS.world(midS).Snapshot()), len(rtP.world(midP).Snapshot()); a != b {
		t.Fatalf("world state differs: serial=%d parallel=%d keys", a, b)
	}

	// the serial run never overlapped; the parallel run did
	if peakSerial != 1 {
		t.Fatalf("serial peak parallelism should be 1, got %d", peakSerial)
	}
	if peakPar < 2 {
		t.Fatalf("parallel run did not overlap — peak parallelism %d", peakPar)
	}

	// and it is faster (serial ~ n*sleep, parallel ~ sleep). Allow generous slack for CI.
	if parWall > serialWall*6/10 {
		t.Fatalf("parallel not faster: serial=%v parallel=%v (peak=%d)", serialWall, parWall, peakPar)
	}
}

func TestDefaultIsSerial(t *testing.T) {
	var peak int32
	rt, mid := buildWaveMission(1, 4, time.Millisecond, &peak) // default path (conc=1)
	rt.Run(mid)
	if rt.effectiveMaxConcurrency() != 1 {
		t.Fatalf("default effective concurrency should be 1, got %d", rt.effectiveMaxConcurrency())
	}
	if peak != 1 {
		t.Fatalf("serial default overlapped, peak=%d", peak)
	}
}
