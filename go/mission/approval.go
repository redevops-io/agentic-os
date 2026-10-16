package mission

// Approval-token helpers shared by the compensation runner and the operator SDK (Go port of the pure parts of
// agentic_os/mission/approval.py: key stripping, canonical JSON, inputs hash).

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
)

var approvalMetaKeys = map[string]bool{"_mission": true, "_goal": true, "_node": true}

// IsApprovalKey: any key whose name, after leading underscores, starts with "approval" (case-insensitive).
func IsApprovalKey(k string) bool {
	return strings.HasPrefix(strings.ToLower(strings.TrimLeft(k, "_")), "approval")
}

// StripApprovalKeys recursively drops every *approval*-prefixed key (a decision must not smuggle a marker in).
func StripApprovalKeys(v any) any {
	switch x := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			if !IsApprovalKey(k) {
				out[k] = StripApprovalKeys(e)
			}
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = StripApprovalKeys(e)
		}
		return out
	}
	return v
}

// canonicalJSON: json.dumps(obj, sort_keys=True, separators=(",", ":")) bytes.
func canonicalJSON(v any) []byte {
	var sb strings.Builder
	writePyJSONSep(&sb, v, ",", ":")
	return []byte(sb.String())
}

// InputsHash: sha256 of the canonical JSON of the inputs minus approval keys and runtime-meta keys (top level only).
func InputsHash(inputs map[string]any) string {
	clean := map[string]any{}
	for k, v := range inputs {
		if !IsApprovalKey(k) && !approvalMetaKeys[k] {
			clean[k] = v
		}
	}
	sum := sha256.Sum256(canonicalJSON(clean))
	return hex.EncodeToString(sum[:])
}
