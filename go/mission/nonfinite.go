package mission

import (
	"bytes"
	"encoding/json"
	"math"
	"strings"
)

// Python's json.dumps writes NaN / Infinity / -Infinity as bare tokens and json.loads reads them back. Go's
// encoding/json refuses both, so an event carrying a non-finite float used to be dropped from the persisted
// log. These helpers keep the on-disk format Python-compatible and lossless for Go.

const (
	phNaN    = "__aos_nonfinite_NaN_7f3c9e1b__"
	phPosInf = "__aos_nonfinite_PosInf_7f3c9e1b__"
	phNegInf = "__aos_nonfinite_NegInf_7f3c9e1b__"
)

func sanitizeNonFinite(v any) any {
	switch x := v.(type) {
	case float64:
		switch {
		case math.IsNaN(x):
			return phNaN
		case math.IsInf(x, 1):
			return phPosInf
		case math.IsInf(x, -1):
			return phNegInf
		}
	case float32:
		return sanitizeNonFinite(float64(x))
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[k] = sanitizeNonFinite(e)
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = sanitizeNonFinite(e)
		}
		return out
	case []float64:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = sanitizeNonFinite(e)
		}
		return out
	}
	return v
}

func restoreNonFinite(v any) any {
	switch x := v.(type) {
	case string:
		switch x {
		case phNaN:
			return math.NaN()
		case phPosInf:
			return math.Inf(1)
		case phNegInf:
			return math.Inf(-1)
		}
	case map[string]any:
		for k, e := range x {
			x[k] = restoreNonFinite(e)
		}
	case []any:
		for i, e := range x {
			x[i] = restoreNonFinite(e)
		}
	}
	return v
}

// encodeEventLine marshals an event; non-finite floats become Python's bare NaN / Infinity / -Infinity.
func encodeEventLine(ev *Event) ([]byte, error) {
	if b, err := json.Marshal(ev); err == nil {
		return b, nil
	}
	cp := *ev
	if cp.Payload != nil {
		cp.Payload = sanitizeNonFinite(cp.Payload).(map[string]any)
	}
	b, err := json.Marshal(&cp)
	if err != nil {
		return nil, err
	}
	s := string(b)
	s = strings.ReplaceAll(s, `"`+phNaN+`"`, "NaN")
	s = strings.ReplaceAll(s, `"`+phPosInf+`"`, "Infinity")
	s = strings.ReplaceAll(s, `"`+phNegInf+`"`, "-Infinity")
	return []byte(s), nil
}

// decodeEventLine parses one JSONL line, accepting Python's bare NaN / Infinity / -Infinity tokens.
func decodeEventLine(line []byte) (*Event, bool) {
	var ev Event
	if json.Unmarshal(line, &ev) != nil {
		fixed, changed := quoteBareNonFinite(line)
		if !changed || json.Unmarshal(fixed, &ev) != nil {
			return nil, false
		}
		if ev.Payload != nil {
			ev.Payload = restoreNonFinite(ev.Payload).(map[string]any)
		}
	}
	return &ev, true
}

// quoteBareNonFinite rewrites bare NaN/Infinity/-Infinity tokens (outside strings) into the placeholders.
func quoteBareNonFinite(in []byte) ([]byte, bool) {
	var out bytes.Buffer
	changed, inStr, esc := false, false, false
	for i := 0; i < len(in); i++ {
		c := in[i]
		if inStr {
			out.WriteByte(c)
			if esc {
				esc = false
			} else if c == '\\' {
				esc = true
			} else if c == '"' {
				inStr = false
			}
			continue
		}
		switch {
		case c == '"':
			inStr = true
			out.WriteByte(c)
		case bytes.HasPrefix(in[i:], []byte("-Infinity")):
			out.WriteString(`"` + phNegInf + `"`)
			i += len("-Infinity") - 1
			changed = true
		case bytes.HasPrefix(in[i:], []byte("Infinity")):
			out.WriteString(`"` + phPosInf + `"`)
			i += len("Infinity") - 1
			changed = true
		case bytes.HasPrefix(in[i:], []byte("NaN")):
			out.WriteString(`"` + phNaN + `"`)
			i += len("NaN") - 1
			changed = true
		default:
			out.WriteByte(c)
		}
	}
	return out.Bytes(), changed
}
