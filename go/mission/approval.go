package mission

// Approval-token helpers shared by the compensation runner and the operator SDK (Go port of the pure parts of
// agentic_os/mission/approval.py: key stripping, canonical JSON, inputs hash).

import (
	"bytes"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"strings"
	"sync"
	"time"
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

// ── signed approval tokens (Go port of approval.py mint/verify/ReplayGuard) ──────────────────────────────────
//
// Token: b64url(payload_json) + "." + b64url(HMAC-SHA256(secret, payload_bytes)). The payload is canonical JSON
// binding operator, capability, mission_id, node_id, inputs_sha256, the approval marker, exp (epoch seconds) and a
// random nonce. Interoperable with the Python implementation byte for byte (shared vectors in testdata). The secret is
// AGENTIC_OS_APPROVAL_SECRET, read at call time; unset/empty => mint returns "" and verify always fails (fail closed).
// Unlike Python there is no platform secrets-file fallback in Go: set the env var.

const EnvApprovalSecret = "AGENTIC_OS_APPROVAL_SECRET"

func approvalSecret(secret []byte) []byte {
	if secret != nil {
		return secret
	}
	return []byte(os.Getenv(EnvApprovalSecret))
}

func normID(v string) any {
	if strings.TrimSpace(v) == "" {
		return nil
	}
	return v
}

func b64(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

// MintApprovalToken returns "" when no secret is configured. missionID/nodeID "" == unbound dimension.
func MintApprovalToken(secret []byte, operator, capability, missionID, nodeID string, inputs, marker map[string]any, ttlSeconds float64) string {
	nonce := make([]byte, 16)
	_, _ = rand.Read(nonce)
	return mintToken(secret, operator, capability, missionID, nodeID, inputs, marker, float64(time.Now().UnixNano())/1e9+ttlSeconds, hex.EncodeToString(nonce))
}

// mintToken is the deterministic core (exp: int64 or float64 as Python would serialise it).
func mintToken(secret []byte, operator, capability, missionID, nodeID string, inputs, marker map[string]any, exp any, nonce string) string {
	key := approvalSecret(secret)
	if len(key) == 0 {
		return ""
	}
	payload := canonicalJSON(map[string]any{"operator": operator, "capability": capability,
		"mission_id": normID(missionID), "node_id": normID(nodeID), "inputs_sha256": InputsHash(inputs),
		"marker": marker, "exp": exp, "nonce": nonce})
	mac := hmac.New(sha256.New, key)
	mac.Write(payload)
	return b64(payload) + "." + b64(mac.Sum(nil))
}

func bindingOK(bound any, presented string) bool {
	b, _ := bound.(string)
	if bound == nil || strings.TrimSpace(b) == "" {
		return true // the minter left this dimension unbound
	}
	return presented != "" && hmac.Equal([]byte(b), []byte(presented))
}

// VerifyApprovalToken returns the approval marker iff the token fully verifies, else nil. Never panics.
// now == 0 means the wall clock.
func VerifyApprovalToken(secret []byte, token, operator, capability string, inputs map[string]any, now float64,
	missionID, nodeID string, guard *ReplayGuard) map[string]any {
	key := approvalSecret(secret)
	if len(key) == 0 || token == "" {
		return nil
	}
	parts := strings.Split(token, ".")
	if len(parts) != 2 {
		return nil
	}
	payload, err1 := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[0], "="))
	sig, err2 := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[1], "="))
	if err1 != nil || err2 != nil {
		return nil
	}
	mac := hmac.New(sha256.New, key)
	mac.Write(payload)
	if !hmac.Equal(mac.Sum(nil), sig) {
		return nil
	}
	dec := json.NewDecoder(bytes.NewReader(payload))
	dec.UseNumber()
	var d map[string]any
	if dec.Decode(&d) != nil {
		return nil
	}
	if now == 0 {
		now = float64(time.Now().UnixNano()) / 1e9
	}
	expN, ok := d["exp"].(json.Number)
	if !ok {
		return nil
	}
	exp, err := expN.Float64()
	if err != nil || !(exp > now) {
		return nil
	}
	if d["operator"] != operator || d["capability"] != capability {
		return nil
	}
	if !bindingOK(d["mission_id"], missionID) || !bindingOK(d["node_id"], nodeID) {
		return nil
	}
	if s, _ := d["inputs_sha256"].(string); !hmac.Equal([]byte(s), []byte(InputsHash(inputs))) {
		return nil
	}
	marker, ok := d["marker"].(map[string]any)
	if !ok {
		return nil
	}
	if guard != nil {
		nonce, _ := d["nonce"].(string)
		if !guard.Consume(nonce, exp, now) {
			return nil
		}
	}
	return marker
}

// ReplayGuard is a bounded in-memory set of used nonces, each kept until its token expires. PER-PROCESS: a
// multi-replica operator needs a shared store behind the same Consume interface.
type ReplayGuard struct {
	mu   sync.Mutex
	used map[string]float64
	max  int
}

func NewReplayGuard(maxEntries int) *ReplayGuard {
	if maxEntries <= 0 {
		maxEntries = 10000
	}
	return &ReplayGuard{used: map[string]float64{}, max: maxEntries}
}

// Consume is true the first time a nonce is seen; false on replay. Fails closed (false) when the table is full of
// unexpired nonces.
func (g *ReplayGuard) Consume(nonce string, exp, now float64) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if len(g.used) >= g.max/2 {
		for n, e := range g.used {
			if e <= now {
				delete(g.used, n)
			}
		}
	}
	if _, dup := g.used[nonce]; dup {
		return false
	}
	if len(g.used) >= g.max {
		return false
	}
	g.used[nonce] = exp
	return true
}
