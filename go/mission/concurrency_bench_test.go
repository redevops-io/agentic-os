package mission

import "testing"

// Illustrative cross-language data point: the per-node cost of the safe-concurrency scheduler (frontier +
// Phase-C conflict detection + release) over an N-node independent wave. Not a head-to-head with the
// Python latency table (different language/runtime); a reproducible number that the Go safe-concurrency
// path is cheap. Run: go test ./mission/ -bench BenchmarkSchedule -benchmem
func benchGraph(n int, keyed bool) *ExecutionGraph {
	g := NewExecutionGraph()
	g.Nodes = make([]Node, n)
	for i := 0; i < n; i++ {
		id := "n" + itoa(i)
		nd := NewNode("cap."+id, "op")
		nd.ID = id
		if keyed { // distinct resource keys → all safe to run together (exercises conflict detection)
			nd.ConcurrencyMode = ModeSideEffecting
			nd.ResourceKeys = []string{"acct:" + id}
		}
		g.Nodes[i] = nd
	}
	return g
}

func benchSchedule(b *testing.B, n int, keyed bool) {
	g := benchGraph(n, keyed)
	pol := SchedulePolicy{MaxConcurrency: n, IsBusinessHours: func() bool { return true }}
	done, running := map[string]bool{}, map[string]bool{}
	s := TopoScheduler{}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = s.Ready(g, done, running, pol)
	}
}

func BenchmarkSchedule20Plain(b *testing.B)   { benchSchedule(b, 20, false) }
func BenchmarkSchedule20Keyed(b *testing.B)   { benchSchedule(b, 20, true) }
func BenchmarkSchedule100Plain(b *testing.B)  { benchSchedule(b, 100, false) }
func BenchmarkSchedule100Keyed(b *testing.B)  { benchSchedule(b, 100, true) }
