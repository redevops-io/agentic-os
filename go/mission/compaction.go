package mission

// Conversation compaction (Go port of compaction.py) — opt-in, off by default. A reusable
// context-management utility: auto-compact at a % threshold, an optional memory flush, an optional
// two-pass fold, and a wall-clock budget. The summarizer is injected; the caller owns *when*.

import "strings"

type Message struct {
	Role    string
	Content string
}

// CompactionPolicy — when + how a session compacts its conversation. Off by default.
type CompactionPolicy struct {
	Enabled                     bool
	AutoCompactThresholdPercent int
	CompactModel                string
	MemoryFlushEnabled          bool
	TwoPassEnabled              bool
	WallClockBudgetSecs         int
	KeepRecent                  int
}

func DefaultCompactionPolicy() CompactionPolicy {
	return CompactionPolicy{Enabled: false, AutoCompactThresholdPercent: 85, WallClockBudgetSecs: 300, KeepRecent: 6}
}

// ShouldCompact reports whether usage crossed the policy threshold (and compaction is enabled).
func ShouldCompact(usedTokens, windowTokens int, p CompactionPolicy) bool {
	if !p.Enabled || windowTokens <= 0 {
		return false
	}
	return float64(usedTokens)/float64(windowTokens)*100 >= float64(p.AutoCompactThresholdPercent)
}

// Compact compacts a message slice → [<memory note?>, <history summary>, recent tail]. summarize is
// the injected model call; note1 is the optional pass-1 speculative prefix summary (two-pass).
// Returns the input unchanged when disabled or already short.
func Compact(messages []Message, p CompactionPolicy, summarize func(string) string, note1 string) []Message {
	if !p.Enabled || len(messages) <= p.KeepRecent {
		return messages
	}
	tail := messages[len(messages)-p.KeepRecent:]
	prefix := messages[:len(messages)-p.KeepRecent]
	var b strings.Builder
	for _, m := range prefix {
		b.WriteString(m.Role + ": " + m.Content + "\n")
	}
	prefixText := b.String()
	out := []Message{}
	if p.MemoryFlushEnabled {
		out = append(out, Message{Role: "system", Content: "MEMORY (kept across compaction):\n" +
			summarize("Extract the durable facts, decisions, and open threads worth keeping:\n"+prefixText)})
	}
	var summary string
	if p.TwoPassEnabled && note1 != "" {
		summary = summarize("Fold this running summary into a final one:\n" + note1 + "\n\nRecent prefix:\n" + prefixText)
	} else {
		summary = summarize("Summarize this conversation so it can be dropped from context:\n" + prefixText)
	}
	out = append(out, Message{Role: "system", Content: "SUMMARY of earlier conversation:\n" + summary})
	return append(out, tail...)
}
