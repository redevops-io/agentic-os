package mission

// The ONE shared classifier of failure-shaped operator results (Go port of verify.py result_failed / is_staged /
// proves_execution). A denylist by design: it must never MINT success from a result that says, in any of the shapes
// real handlers use, that it did not succeed. proves_execution is the STRICT positive check used to authorise
// compensation.

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

var failedStatuses = setOf("error", "err", "failed", "failure", "forbidden", "unauthorized", "unavailable", "timeout",
	"conflict", "denied", "rejected", "blocked", "invalid", "not_found", "unverifiable", "pending_approval",
	"awaiting_approval", "awaiting_connector", "partial", "undeliverable", "not_configured")
var stagedStatuses = setOf("pending_approval", "awaiting_approval", "awaiting_connector")
var notExecutedStatuses = setOf("pending_approval", "awaiting_approval", "awaiting_connector", "staged", "held", "pending", "queued")
var codeKeys = setOf("status_code", "http_status", "http_code", "status")
var httpErrorDetails = setOf("forbidden", "unauthorized", "not found", "not authenticated", "bad request", "method not allowed",
	"conflict", "unprocessable entity", "too many requests", "internal server error", "bad gateway",
	"service unavailable", "gateway timeout", "request timeout", "payment required", "invalid token",
	"could not validate credentials", "authentication failed", "permission denied", "access denied")

const classifyMaxDepth = 3

var nestedKeys = []string{"results", "items"}

func setOf(xs ...string) map[string]bool {
	m := make(map[string]bool, len(xs))
	for _, x := range xs {
		m[x] = true
	}
	return m
}

// pyTruthy mirrors Python truthiness for JSON-decoded values.
func pyTruthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case float64:
		return x != 0
	case int:
		return x != 0
	case int64:
		return x != 0
	case json.Number:
		f, _ := x.Float64()
		return f != 0
	case []any:
		return len(x) > 0
	case []string:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}

func lowerTrim(v any) string { s, _ := v.(string); return strings.ToLower(strings.TrimSpace(s)) }

func asIntLoose(v any) (int, bool) {
	switch x := v.(type) {
	case bool:
		return 0, false
	case int:
		return x, true
	case int64:
		return int(x), true
	case float64:
		return int(x), true
	case json.Number:
		n, err := x.Float64()
		return int(n), err == nil
	}
	return 0, false
}

func asCount(v any) int {
	if n, ok := asIntLoose(v); ok {
		return n
	}
	if s, ok := v.(string); ok {
		t := strings.TrimSpace(s)
		if t2 := strings.TrimLeft(t, "-"); t2 != "" && strings.Trim(t2, "0123456789") == "" {
			n, _ := strconv.Atoi(t)
			return n
		}
	}
	return 0
}

func isHTTPErrorValue(k string, v any) bool {
	if s, ok := v.(string); ok {
		t := strings.TrimSpace(s)
		if t != "" && strings.Trim(t, "0123456789") == "" {
			n, _ := strconv.Atoi(t)
			v = n
		}
	}
	if _, isB := v.(bool); isB {
		return false
	}
	n, ok := asIntLoose(v)
	if !ok || n < 400 {
		return false
	}
	lk := strings.ToLower(k)
	return codeKeys[lk] || strings.HasSuffix(lk, "_status") || strings.HasSuffix(lk, "_status_code") || strings.HasSuffix(lk, "_http_status")
}

// IsStaged: the result says 'staged / awaiting approval'.
func IsStaged(result any) bool {
	m, ok := result.(map[string]any)
	return ok && stagedStatuses[lowerTrim(m["status"])]
}

func classify(result map[string]any, depth int) (bool, string) {
	if st, ok := result["status"].(string); ok && failedStatuses[strings.ToLower(strings.TrimSpace(st))] {
		return true, "status=" + strings.ToLower(strings.TrimSpace(st))
	}
	if b, ok := result["ok"].(bool); ok && !b {
		return true, "ok is False"
	}
	if b, ok := result["success"].(bool); ok && !b {
		return true, "success is False"
	}
	if b, ok := result["executed"].(bool); ok && !b {
		return true, "executed is False"
	}
	if b, ok := result["verified"].(bool); ok && !b {
		return true, "verified is False"
	}
	for _, k := range []string{"error", "errors"} {
		if pyTruthy(result[k]) {
			return true, k + " present"
		}
	}
	if asCount(result["failed_count"]) > 0 {
		return true, fmt.Sprintf("failed_count=%v", result["failed_count"])
	}
	if pyTruthy(result["failures"]) {
		return true, "failures present"
	}
	if d, ok := result["detail"].(string); ok && httpErrorDetails[strings.TrimRight(strings.ToLower(strings.TrimSpace(d)), ".")] {
		return true, fmt.Sprintf("detail='%s' (HTTP-style error body)", strings.TrimSpace(d))
	}
	for k, v := range result {
		if isHTTPErrorValue(k, v) {
			return true, fmt.Sprintf("%s=%v", k, v)
		}
	}
	if depth < classifyMaxDepth {
		for _, nk := range nestedKeys {
			if seq, ok := result[nk].([]any); ok {
				for i, el := range seq {
					if em, ok := el.(map[string]any); ok {
						if bad, why := classify(em, depth+1); bad {
							return true, fmt.Sprintf("%s[%d]: %s", nk, i, why)
						}
					}
				}
			}
		}
	}
	return false, ""
}

// ResultFailed -> (failed, reason). A side-effecting capability that declares no outputs must report SOMETHING.
func ResultFailed(result any, sideEffecting, declaresOutputs bool) (bool, string) {
	m, ok := result.(map[string]any)
	if !ok {
		return true, "result is not a dict"
	}
	if sideEffecting && !declaresOutputs && len(m) == 0 {
		return true, "side-effecting capability returned an empty result"
	}
	return classify(m, 0)
}

// ProvesExecution: STRICT check used to authorise compensation - true only if the result positively looks like an
// executed effect.
func ProvesExecution(result any) bool { return provesExecution(result, 0) }

func provesExecution(result any, depth int) bool {
	m, ok := result.(map[string]any)
	if !ok || len(m) == 0 || depth > classifyMaxDepth {
		return false
	}
	if pyTruthy(m["staged"]) {
		return false
	}
	if b, ok := m["executed"].(bool); ok && !b {
		return false
	}
	if notExecutedStatuses[lowerTrim(m["status"])] {
		return false
	}
	if IsStaged(m) {
		return false
	}
	if bad, _ := classify(m, 0); bad {
		return false
	}
	if nested, ok := m["result"].(map[string]any); ok && !provesExecution(nested, depth+1) {
		return false
	}
	for _, nk := range nestedKeys {
		if seq, ok := m[nk].([]any); ok {
			for _, el := range seq {
				if em, ok := el.(map[string]any); ok && !provesExecution(em, depth+1) {
					return false
				}
			}
		}
	}
	return true
}
