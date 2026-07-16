package mission

import (
	"fmt"
	"strings"
	"testing"
)

func TestCompactionOffByDefault(t *testing.T) {
	p := DefaultCompactionPolicy()
	if p.Enabled {
		t.Fatal("compaction should be off by default")
	}
	msgs := make([]Message, 20)
	for i := range msgs {
		msgs[i] = Message{Role: "user", Content: fmt.Sprintf("m%d", i)}
	}
	if len(Compact(msgs, p, func(string) string { return "S" }, "")) != len(msgs) {
		t.Fatal("disabled → unchanged")
	}
	if ShouldCompact(999, 1000, p) {
		t.Fatal("disabled → never triggers")
	}
}

func TestCompactionSummarizesKeepsTailFlushesMemory(t *testing.T) {
	p := DefaultCompactionPolicy()
	p.Enabled, p.KeepRecent, p.MemoryFlushEnabled = true, 2, true
	msgs := make([]Message, 6)
	for i := range msgs {
		msgs[i] = Message{Role: "user", Content: fmt.Sprintf("m%d", i)}
	}
	out := Compact(msgs, p, func(string) string { return "S" }, "")
	if len(out) >= len(msgs) {
		t.Fatal("should have freed context")
	}
	if out[len(out)-1].Content != "m5" || out[len(out)-2].Content != "m4" {
		t.Fatal("recent tail not kept verbatim")
	}
	head := out[0].Content + out[1].Content
	if !strings.Contains(head, "MEMORY") || !strings.Contains(head, "SUMMARY") {
		t.Fatalf("memory-flush + summary expected, got %q", head)
	}
	if !ShouldCompact(90, 100, CompactionPolicy{Enabled: true, AutoCompactThresholdPercent: 85}) {
		t.Fatal("90%% should trigger")
	}
}
