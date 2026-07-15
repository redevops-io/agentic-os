package mission

import (
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
)

func itoa(n int) string { return strconv.Itoa(n) }

// toStrSlice coerces a []string or []any (from a reloaded JSON payload) into []string.
func toStrSlice(v any) []string {
	switch xs := v.(type) {
	case []string:
		return xs
	case []any:
		out := make([]string, 0, len(xs))
		for _, x := range xs {
			if s, ok := x.(string); ok {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}

var nonAlnum = regexp.MustCompile(`[^a-z0-9]+`)

// slugify lowercases, collapses non-alphanumerics to "_", trims "_", truncates to maxLen —
// mirrors the compiler/planner re.sub slug.
func slugify(text string, maxLen int) string {
	s := strings.Trim(nonAlnum.ReplaceAllString(strings.ToLower(text), "_"), "_")
	if len(s) > maxLen {
		s = s[:maxLen]
	}
	return s
}

func b2i(b bool) int {
	if b {
		return 1
	}
	return 0
}

func minFloat(vs []float64) float64 {
	m := vs[0]
	for _, v := range vs[1:] {
		if v < m {
			m = v
		}
	}
	return m
}

// toFloat coerces a JSON-ish scalar to float64 (payloads round-trip ints as float64), with a default.
func toFloat(v any, def float64) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case float32:
		return float64(x)
	case int:
		return float64(x)
	case int64:
		return float64(x)
	case nil:
		return def
	default:
		return def
	}
}

// roundN rounds to n decimal places (mirrors Python round()).
func roundN(x float64, n int) float64 {
	p := math.Pow(10, float64(n))
	return math.Round(x*p) / p
}

func strContains(s []string, x string) bool {
	for _, v := range s {
		if v == x {
			return true
		}
	}
	return false
}

// mapKeys returns a map's keys (order unspecified — callers that need determinism must sort).
func mapKeys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

// hashKey is a stable grouping key for an arbitrary value (the Go analogue of Python's
// _hashable(v): the value when trivially encodable, else its printed form).
func hashKey(v any) string {
	if b, err := json.Marshal(v); err == nil {
		return string(b)
	}
	return fmt.Sprintf("%v", v)
}

// jsonableMap renders a struct/value as a map[string]any (for event payloads).
func jsonableMap(v any) map[string]any {
	if m, ok := jsonable(v).(map[string]any); ok {
		return m
	}
	return map[string]any{}
}

// truthy mirrors Python bool(v): nil/false/""/0/empty → false.
func truthy(v any) bool {
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
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	default:
		return true
	}
}
