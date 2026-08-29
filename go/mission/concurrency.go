package mission

// Canonical concurrency safety semantics — the conflict model the scheduler consults. Go port of
// concurrency.py. A single parallel_safe:bool is too weak: two read-only searches run together; two
// writes to different accounts run together; two writes to the same account must not. So conflict is
// expressed with resource/conflict keys plus a mode, never a global flag. The scheduler (Phase C) and the
// runtime telemetry both call this, so the safety rules live in one auditable place.

import (
	"strconv"
	"strings"
)

// Concurrency modes. Read-only work never conflicts on its keys — only writers/exclusive holders lock.
const (
	ModeReadOnly      = "read_only"
	ModeIdempotent    = "idempotent"
	ModeSideEffecting = "side_effecting"
	ModeExclusive     = "exclusive"
)

// holdsLock reports whether the node takes a resource lock at all. Read-only capabilities (and
// unclassified ones with no declared keys) don't — so two searches over the same corpus run together.
func holdsLock(n *Node) bool {
	if n.ConcurrencyMode == ModeReadOnly {
		return false
	}
	return len(resourceKeys(n)) > 0
}

// resourceKeys returns the conflict keys this node holds while running: static ResourceKeys plus a
// ConcurrencyKey template resolved against the node's concrete inputs. An unresolved placeholder falls
// back to the raw template, so two nodes sharing an unresolved template still serialize (safer to
// over-serialize than to miss a real conflict).
func resourceKeys(n *Node) []string {
	keys := make([]string, 0, len(n.ResourceKeys)+1)
	keys = append(keys, n.ResourceKeys...)
	if n.ConcurrencyKey != "" {
		keys = append(keys, resolveTemplate(n.ConcurrencyKey, n.Inputs))
	}
	seen := map[string]bool{}
	out := make([]string, 0, len(keys))
	for _, k := range keys {
		if k != "" && !seen[k] {
			seen[k] = true
			out = append(out, k)
		}
	}
	return out
}

// resolveTemplate: "crm:account:{account_id}" + {account_id:123} → "crm:account:123". Only concrete
// scalar inputs substitute; an unresolved {ph} is left verbatim (conservative — see resourceKeys).
func resolveTemplate(tmpl string, inputs map[string]any) string {
	if !strings.ContainsRune(tmpl, '{') {
		return tmpl
	}
	var b strings.Builder
	for i := 0; i < len(tmpl); {
		if tmpl[i] == '{' {
			if j := strings.IndexByte(tmpl[i:], '}'); j >= 0 {
				name := tmpl[i+1 : i+j]
				if v, ok := inputs[name]; ok {
					if s, ok := scalar(v); ok {
						b.WriteString(s)
						i += j + 1
						continue
					}
				}
				b.WriteString(tmpl[i : i+j+1]) // leave "{name}" verbatim
				i += j + 1
				continue
			}
		}
		b.WriteByte(tmpl[i])
		i++
	}
	return b.String()
}

func scalar(v any) (string, bool) {
	switch x := v.(type) {
	case string:
		return x, true
	case bool:
		if x {
			return "true", true
		}
		return "false", true
	case int:
		return itoa(x), true
	case int64:
		return itoa(int(x)), true
	case float64:
		// integer-valued floats render without a trailing ".0" (JSON numbers arrive as float64)
		if x == float64(int64(x)) {
			return itoa(int(x)), true
		}
		return strconv.FormatFloat(x, 'g', -1, 64), true
	}
	return "", false
}

// keyLimit returns the max concurrent holders of this node's key(s): MaxParallelism if set, else 1.
func keyLimit(n *Node) int {
	if n.MaxParallelism > 0 {
		return n.MaxParallelism
	}
	return 1
}

// conflict reports whether releasing n now would violate a resource limit, given inflight (key → holders
// already running or released this wave). Returns an auditable reason, or "" if the node is safe.
func conflict(n *Node, inflight map[string]int) string {
	if !holdsLock(n) {
		return ""
	}
	limit := keyLimit(n)
	for _, k := range resourceKeys(n) {
		if inflight[k] >= limit {
			return "resource_key=" + k + " at limit " + itoa(limit)
		}
	}
	return ""
}

// acquire records that n now holds its keys (mutates inflight). No-op for lock-free nodes.
func acquire(n *Node, inflight map[string]int) {
	if !holdsLock(n) {
		return
	}
	for _, k := range resourceKeys(n) {
		inflight[k]++
	}
}
