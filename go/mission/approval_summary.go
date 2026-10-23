package mission

// What an approver is shown about an action, the digest an approval is bound to, and when a parked gate lapses.
// Go port of agentic_os/mission/approval.py (action_summary_of, approval_expired) and runtime._inputs_digest, asserted
// against the shared vectors (conformance/vectors/{action_summary,inputs_digest,approval_ttl}.json).

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"unicode/utf8"
)

// SummaryKeys is the allowlist of input names an approver sees (never free text, never secrets).
var SummaryKeys = []string{"amount", "currency", "total", "quantity", "count", "channel", "subject", "to", "recipient",
	"destination", "invoice_id", "account_id", "customer_id", "order_id", "resource"}

// pySpace reports whether r matches Python's regex \s for str patterns (str.isspace): ASCII whitespace plus
// \x1c-\x1f, \x85, \xa0 and the Unicode space separators. unicode.IsSpace differs (it omits \x1c-\x1f).
func pySpace(r rune) bool {
	switch {
	case r >= 0x09 && r <= 0x0d, r >= 0x1c && r <= 0x20:
		return true
	case r == 0x85, r == 0xa0, r == 0x1680, r >= 0x2000 && r <= 0x200a, r == 0x2028, r == 0x2029, r == 0x202f,
		r == 0x205f, r == 0x3000:
		return true
	}
	return false
}

// maskEmails is re.sub(r"([^@\s]{1,2})[^@\s]*@", r"\1***@", s) with Python's \s semantics: for each "@", the run of
// non-space, non-@ characters before it is replaced by its first two characters followed by ***.
func maskEmails(s string) string {
	rs := []rune(s)
	var out strings.Builder
	i := 0
	for i < len(rs) {
		// a match starts at the leftmost position where [^@\s]+ is followed by "@"
		if rs[i] == '@' || pySpace(rs[i]) {
			out.WriteRune(rs[i])
			i++
			continue
		}
		j := i
		for j < len(rs) && rs[j] != '@' && !pySpace(rs[j]) {
			j++
		}
		if j < len(rs) && rs[j] == '@' { // run of >=1 chars then "@"
			keep := j - i
			if keep > 2 {
				keep = 2
			}
			out.WriteString(string(rs[i : i+keep]))
			out.WriteString("***@")
			i = j + 1
			continue
		}
		out.WriteString(string(rs[i:j]))
		i = j
	}
	return out.String()
}

func truncRunes(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n])
}

// ActionSummaryOf is the concrete facts shown to an approver from the inputs about to run: strings truncated to 80
// characters with e-mail local parts masked, numbers and booleans as they are, null as "null", anything else as its
// canonical JSON text truncated to 80.
func ActionSummaryOf(resolved map[string]any) map[string]any {
	out := map[string]any{}
	for _, k := range SummaryKeys {
		v, ok := resolved[k]
		if !ok {
			continue
		}
		switch x := v.(type) {
		case string:
			out[k] = truncRunes(maskEmails(x), 80)
		case bool, int, int64, float64, json.Number:
			out[k] = x
		case nil:
			out[k] = "null"
		default:
			var sb strings.Builder
			writePyJSONOpts(&sb, normalizeAny(x), ",", ":", false)
			out[k] = truncRunes(sb.String(), 80)
		}
	}
	return out
}

// normalizeAny round-trips a value through encoding/json with UseNumber so structs/typed slices render as plain JSON.
func normalizeAny(v any) any {
	b, err := json.Marshal(v)
	if err != nil {
		return v
	}
	var out any
	d := json.NewDecoder(strings.NewReader(string(b)))
	d.UseNumber()
	if d.Decode(&out) != nil {
		return v
	}
	return out
}

// ApprovalExpired: a parked gate lapses when ttl > 0 and it has waited longer than that (ttl 0 = no limit).
func ApprovalExpired(parkedAt *float64, now, ttlS float64) bool {
	return ttlS != 0 && parkedAt != nil && now-*parkedAt > ttlS
}

// RuntimeInputsDigest is what NodeParked / ApprovalGranted record: sha256 of the canonical JSON (ensure_ascii=True)
// of the inputs with approval keys stripped at every level. Unlike InputsHash it KEEPS _mission and _goal (they are
// constant per mission). The caller removes _approval and _node first.
func RuntimeInputsDigest(inputs map[string]any) string {
	clean, _ := StripApprovalKeys(normalizeNumbers(inputs)).(map[string]any)
	sum := sha256.Sum256(canonicalJSON(clean))
	return hex.EncodeToString(sum[:])
}
