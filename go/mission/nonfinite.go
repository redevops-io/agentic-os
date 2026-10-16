package mission

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"reflect"
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

var jsonMarshalerType = reflect.TypeOf((*json.Marshaler)(nil)).Elem()

// sanitizeNonFinite returns a JSON-able copy of v in which every non-finite float, at any depth and in any container
// shape (maps, slices, arrays, pointers, structs with json tags), is replaced by a placeholder string. Values that
// marshal themselves (json.Marshaler) and leaf kinds are passed through untouched.
func sanitizeNonFinite(v any) any { return sanitizeValue(reflect.ValueOf(v)) }

func sanitizeValue(v reflect.Value) any {
	if !v.IsValid() {
		return nil
	}
	if v.Type().Implements(jsonMarshalerType) || (v.CanAddr() && reflect.PtrTo(v.Type()).Implements(jsonMarshalerType)) {
		return v.Interface()
	}
	switch v.Kind() {
	case reflect.Ptr, reflect.Interface:
		if v.IsNil() {
			return nil
		}
		return sanitizeValue(v.Elem())
	case reflect.Float32, reflect.Float64:
		x := v.Float()
		switch {
		case math.IsNaN(x):
			return phNaN
		case math.IsInf(x, 1):
			return phPosInf
		case math.IsInf(x, -1):
			return phNegInf
		}
		return x
	case reflect.Map:
		if v.IsNil() {
			return nil
		}
		out := make(map[string]any, v.Len())
		for _, k := range v.MapKeys() {
			out[fmt.Sprint(k.Interface())] = sanitizeValue(v.MapIndex(k))
		}
		return out
	case reflect.Slice, reflect.Array:
		if v.Kind() == reflect.Slice && v.IsNil() {
			return nil
		}
		if v.Type().Elem().Kind() == reflect.Uint8 {
			return v.Interface()
		}
		out := make([]any, v.Len())
		for i := range out {
			out[i] = sanitizeValue(v.Index(i))
		}
		return out
	case reflect.Struct:
		out := map[string]any{}
		t := v.Type()
		for i := 0; i < t.NumField(); i++ {
			f := t.Field(i)
			if f.PkgPath != "" { // unexported
				continue
			}
			name, omit := f.Name, false
			if tag, ok := f.Tag.Lookup("json"); ok {
				parts := strings.Split(tag, ",")
				if parts[0] == "-" && len(parts) == 1 {
					continue
				}
				if parts[0] != "" {
					name = parts[0]
				}
				for _, o := range parts[1:] {
					omit = omit || o == "omitempty"
				}
			}
			fv := v.Field(i)
			if omit && fv.IsZero() {
				continue
			}
			out[name] = sanitizeValue(fv)
		}
		return out
	}
	return v.Interface()
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
		if m, ok := sanitizeNonFinite(cp.Payload).(map[string]any); ok {
			cp.Payload = m
		}
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
