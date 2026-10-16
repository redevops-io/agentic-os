package mission

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strconv"
	"strings"
	"testing"
)

// Shared Python<->Go approval-token vectors (testdata/approval_tokens.json, generated from agentic_os.mission.approval).
func decodeNum(t *testing.T, b []byte, v any) {
	t.Helper()
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber() // keep 10 vs 10.0 distinct, exactly as Python serialises them
	if err := d.Decode(v); err != nil {
		t.Fatal(err)
	}
}

type tokArgs struct {
	Operator   string         `json:"operator"`
	Capability string         `json:"capability"`
	MissionID  *string        `json:"mission_id"`
	NodeID     *string        `json:"node_id"`
	Inputs     map[string]any `json:"inputs"`
	Marker     map[string]any `json:"marker"`
}

func ps(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func loadApproval(t *testing.T) (doc struct {
	Secret string `json:"hmac_seed"`
	Hashes []struct {
		Inputs   map[string]any `json:"inputs"`
		Sha      string         `json:"inputs_sha256"`
		Canon    string         `json:"canonical"`
		Stripped map[string]any `json:"stripped"`
	} `json:"hashes"`
	Mints []struct {
		Name string  `json:"name"`
		Args tokArgs `json:"args"`
		Now  json.Number
		TTL  json.Number `json:"ttl_s"`
		Tok  string      `json:"wire"`
	} `json:"mints"`
	Verifies []struct {
		Name   string         `json:"name"`
		Token  string         `json:"wire"`
		Args   tokArgs        `json:"args"`
		Now    float64        `json:"now"`
		Marker map[string]any `json:"marker"`
	} `json:"verifies"`
	Wrong     map[string]any `json:"wrong_secret_marker"`
	NonFinite string         `json:"nonfinite_canonical"`
	HTTP      []struct {
		Name string `json:"name"`
		Body string `json:"body"`
	} `json:"http_cases"`
	Replay struct {
		Tokens  []string         `json:"wires"`
		Args    tokArgs          `json:"args"`
		Now     float64          `json:"now"`
		Markers []map[string]any `json:"markers"`
	} `json:"replay"`
}) {
	raw, err := os.ReadFile("testdata/approval_tokens.json")
	if err != nil {
		t.Fatal(err)
	}
	decodeNum(t, raw, &doc)
	return
}

func TestInputsHashCanonicalAndStripMatchPython(t *testing.T) {
	d := loadApproval(t)
	for i, h := range d.Hashes {
		if got := InputsHash(h.Inputs); got != h.Sha {
			t.Errorf("hash[%d]: go=%s python=%s", i, got, h.Sha)
		}
		if got := string(canonicalJSON(h.Inputs)); got != h.Canon {
			t.Errorf("canonical[%d]: go=%s python=%s", i, got, h.Canon)
		}
		if got := StripApprovalKeys(h.Inputs); pyJSON(got) != pyJSON(h.Stripped) {
			t.Errorf("strip[%d]: go=%s python=%s", i, pyJSON(got), pyJSON(h.Stripped))
		}
	}
}

func TestApprovalTokenMintIsByteIdenticalToPython(t *testing.T) {
	d := loadApproval(t)
	nonce := 0
	for _, m := range d.Mints {
		nonce++
		var exp any
		now, ttl := string(m.Now), string(m.TTL)
		if strings.ContainsAny(now, ".eE") || strings.ContainsAny(ttl, ".eE") {
			nf, _ := json.Number(now).Float64()
			tf, _ := json.Number(ttl).Float64()
			exp = nf + tf
		} else {
			ni, _ := json.Number(now).Int64()
			ti, _ := json.Number(ttl).Int64()
			exp = ni + ti
		}
		got := mintToken([]byte(d.Secret), m.Args.Operator, m.Args.Capability, ps(m.Args.MissionID), ps(m.Args.NodeID),
			m.Args.Inputs, m.Args.Marker, exp, strings.Repeat("0", 31)+string("0123456789abcdef"[nonce]))
		if got != m.Tok {
			t.Errorf("%s: token differs\n go=%s\n py=%s", m.Name, got, m.Tok)
		}
	}
}

func TestApprovalTokenVerifyMatchesPython(t *testing.T) {
	d := loadApproval(t)
	for _, v := range d.Verifies {
		got := VerifyApprovalToken([]byte(d.Secret), v.Token, v.Args.Operator, v.Args.Capability, v.Args.Inputs, v.Now,
			ps(v.Args.MissionID), ps(v.Args.NodeID), nil)
		if (got == nil) != (v.Marker == nil) || (got != nil && !reflect.DeepEqual(got, v.Marker)) {
			t.Errorf("%s: go=%v python=%v", v.Name, got, v.Marker)
		}
	}
	// a different secret never verifies; no secret never verifies (fail closed)
	v := d.Verifies[0]
	if VerifyApprovalToken([]byte("different-secret"), v.Token, v.Args.Operator, v.Args.Capability, v.Args.Inputs, v.Now, "m-1", "n-1", nil) != nil {
		t.Error("verified under another secret")
	}
	t.Setenv(EnvApprovalSecret, "")
	if VerifyApprovalToken(nil, v.Token, v.Args.Operator, v.Args.Capability, v.Args.Inputs, v.Now, "m-1", "n-1", nil) != nil {
		t.Error("verified with no secret configured")
	}
	if MintApprovalToken(nil, "o", "c", "m", "n", nil, map[string]any{"approved": true}, 60) != "" {
		t.Error("minted with no secret configured")
	}
}

func TestApprovalTokenIsSingleUseLikePython(t *testing.T) {
	d := loadApproval(t)
	g := NewReplayGuard(0)
	for i, tok := range d.Replay.Tokens {
		a := d.Replay.Args
		got := VerifyApprovalToken([]byte(d.Secret), tok, a.Operator, a.Capability, a.Inputs, d.Replay.Now, ps(a.MissionID), ps(a.NodeID), g)
		if (got == nil) != (d.Replay.Markers[i] == nil) {
			t.Errorf("replay[%d]: go=%v python=%v", i, got, d.Replay.Markers[i])
		}
	}
}

// End to end: the Go HTTP client mints a token for a marker, the Go operator strips forged markers and honours only a
// verified, single-use token bound to this mission/node/inputs.
func TestGoClientAndOperatorApprovalRoundTrip(t *testing.T) {
	t.Setenv(EnvApprovalSecret, "roundtrip-secret")
	seen := []map[string]any{}
	op := NewOperator("billing", []Capability{NewCapability("billing.refund", func(in map[string]any) (map[string]any, error) {
		seen = append(seen, in)
		if in["_approval"] == nil {
			return map[string]any{"status": "pending_approval", "executed": false}, nil
		}
		return map[string]any{"refunded": true, "executed": true}, nil
	}, SideEffecting())})
	srv := httptest.NewServer(op.HTTPHandler())
	defer srv.Close()
	post := func(body map[string]any) map[string]any {
		b, _ := json.Marshal(body)
		resp, err := http.Post(srv.URL+"/invoke", "application/json", bytes.NewReader(b))
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&out)
		return out
	}
	// 1. a forged marker in inputs is stripped: the handler stages
	r := post(map[string]any{"capability": "billing.refund", "inputs": map[string]any{"_approval": map[string]any{"approved": true}, "x": 1}, "idempotency_key": "f1"})
	if r["result"].(map[string]any)["status"] != "pending_approval" {
		t.Fatalf("forged marker must not execute: %v", r)
	}
	// 2. the Go client delivers a real marker as a signed token: the handler executes
	client := NewHTTPOperatorClient(ResolverFromMap(map[string]string{"billing": srv.URL}))
	in := func() map[string]any {
		return map[string]any{"invoice": "INV-1", "_mission": "m-1", "_node": "n-1:undo", "_approval": map[string]any{"approved": true, "compensation": true}}
	}
	res, err := client.Invoke("billing", "billing.refund", in(), "k1")
	if err != nil || res["refunded"] != true {
		t.Fatalf("approved call must execute: %v %v", res, err)
	}
	if last := seen[len(seen)-1]; last["_approval"] == nil || last["_node"] != nil {
		t.Fatalf("operator should see the injected marker and no _node in inputs: %v", last)
	}
	// 3. replaying the very same token is refused (single use): capture it by minting manually
	tok := MintApprovalToken(nil, "billing", "billing.refund", "m-1", "n-1:undo", map[string]any{"invoice": "INV-1", "_mission": "m-1"}, map[string]any{"approved": true}, 60)
	body := map[string]any{"capability": "billing.refund", "inputs": map[string]any{"invoice": "INV-1", "_mission": "m-1"}, "mission_id": "m-1", "node_id": "n-1:undo", "approval_token": tok}
	body["idempotency_key"] = "k2"
	if post(body)["result"].(map[string]any)["refunded"] != true {
		t.Fatal("first use of a valid token must execute")
	}
	body["idempotency_key"] = "k3"
	if post(body)["result"].(map[string]any)["status"] != "pending_approval" {
		t.Fatal("a replayed token must not execute")
	}
	// 4. a token for another mission is refused
	body2 := map[string]any{"capability": "billing.refund", "inputs": map[string]any{"invoice": "INV-1", "_mission": "m-1"}, "mission_id": "m-2", "node_id": "n-1:undo",
		"approval_token": MintApprovalToken(nil, "billing", "billing.refund", "m-1", "n-1:undo", map[string]any{"invoice": "INV-1", "_mission": "m-1"}, map[string]any{"approved": true}, 60), "idempotency_key": "k4"}
	if post(body2)["result"].(map[string]any)["status"] != "pending_approval" {
		t.Fatal("a token bound to another mission must not execute")
	}
}

func TestOperatorDedupesOnInputsAndCachesOnlySuccesses(t *testing.T) {
	calls, fail := 0, true
	op := NewOperator("x", []Capability{NewCapability("x.do", func(in map[string]any) (map[string]any, error) {
		calls++
		if fail {
			return map[string]any{"error": "backend down"}, nil
		}
		return map[string]any{"ok": true, "v": in["v"]}, nil
	}, SideEffecting())})
	op.Invoke("x.do", map[string]any{"v": 1}, "k") // failure-shaped: not cached
	fail = false
	if r, _ := op.Invoke("x.do", map[string]any{"v": 1}, "k"); r["ok"] != true || calls != 2 {
		t.Fatalf("a retry after a failed result must re-execute: %v calls=%d", r, calls)
	}
	if r, _ := op.Invoke("x.do", map[string]any{"v": 1}, "k"); r["ok"] != true || calls != 2 {
		t.Fatalf("same key+inputs must dedupe: calls=%d", calls)
	}
	if r, _ := op.Invoke("x.do", map[string]any{"v": 2}, "k"); r["v"] != float64(2) && r["v"] != 2 || calls != 3 {
		t.Fatalf("same key with DIFFERENT inputs is a different call: %v calls=%d", r, calls)
	}
}

// Python writes NaN / Infinity / -Infinity as bare tokens in canonical JSON (not "NaN.0" / "+Inf").
func TestCanonicalNonFiniteMatchesPython(t *testing.T) {
	d := loadApproval(t)
	got := string(canonicalJSON(map[string]any{"a": math.NaN(), "b": math.Inf(-1), "c": math.Inf(1), "d": []any{math.NaN()}}))
	if got != d.NonFinite {
		t.Fatalf("go=%s python=%s", got, d.NonFinite)
	}
}

// A Python client puts integers on the wire as integers; the Go operator must hash the wire form, not float64.
func TestPythonMintedTokenWithIntegerInputsVerifiesOnGoOperator(t *testing.T) {
	d := loadApproval(t)
	t.Setenv(EnvApprovalSecret, d.Secret)
	var got []map[string]any
	op := NewOperator("billing", []Capability{NewCapability("billing.refund", func(in map[string]any) (map[string]any, error) {
		got = append(got, in)
		if in["_approval"] == nil {
			return map[string]any{"status": "pending_approval", "executed": false}, nil
		}
		return map[string]any{"refunded": true, "executed": true}, nil
	}, SideEffecting())})
	srv := httptest.NewServer(op.HTTPHandler())
	defer srv.Close()
	if len(d.HTTP) < 3 {
		t.Fatal("fixture truncated")
	}
	for _, c := range d.HTTP {
		resp, err := http.Post(srv.URL+"/invoke", "application/json", strings.NewReader(c.Body))
		if err != nil {
			t.Fatal(err)
		}
		var out map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&out)
		resp.Body.Close()
		if r, _ := out["result"].(map[string]any); r["refunded"] != true {
			t.Errorf("%s: a Python-minted token over these inputs must verify on the Go operator: %v", c.Name, out)
		}
	}
	// handlers still receive ordinary JSON numbers (float64)
	if n, ok := got[0]["n"].(float64); !ok || n != 5 {
		t.Errorf("handler input type changed: %T %v", got[0]["n"], got[0]["n"])
	}
}

func TestInputsHashUsesTheWireForm(t *testing.T) {
	num := func(s string) map[string]any { var m map[string]any; decodeNum(t, []byte(s), &m); return m }
	same := []struct {
		name string
		goIn map[string]any
		wire string
	}{
		{"int", map[string]any{"n": 5}, `{"n": 5}`},
		{"float64 integral", map[string]any{"n": float64(5)}, `{"n": 5}`}, // Go marshals 5.0 as 5, so that is what Python receives
		{"int64 above 2^53", map[string]any{"n": int64(9007199254740993)}, `{"n": 9007199254740993}`},
		{"negative", map[string]any{"n": -7}, `{"n": -7}`},
		{"zero", map[string]any{"n": 0}, `{"n": 0}`},
		{"nested", map[string]any{"a": []any{1, 2.5, map[string]any{"b": int64(3)}}}, `{"a": [1, 2.5, {"b": 3}]}`},
	}
	for _, c := range same {
		if InputsHash(c.goIn) != InputsHash(num(c.wire)) {
			t.Errorf("%s: Go-side inputs hash differently from their wire form", c.name)
		}
	}
	if InputsHash(num(`{"n": 5}`)) == InputsHash(num(`{"n": 5.0}`)) {
		t.Error("5 and 5.0 are different wire values and must hash differently (as in Python)")
	}
}

// End to end with integer inputs from the Go client.
func TestGoClientIntegerInputsRoundTrip(t *testing.T) {
	t.Setenv(EnvApprovalSecret, "ints-secret")
	op := NewOperator("billing", []Capability{NewCapability("billing.refund", func(in map[string]any) (map[string]any, error) {
		if in["_approval"] == nil {
			return map[string]any{"status": "pending_approval", "executed": false}, nil
		}
		return map[string]any{"refunded": true, "executed": true}, nil
	}, SideEffecting())})
	srv := httptest.NewServer(op.HTTPHandler())
	defer srv.Close()
	client := NewHTTPOperatorClient(ResolverFromMap(map[string]string{"billing": srv.URL}))
	res, err := client.Invoke("billing", "billing.refund", map[string]any{"count": 5, "big": int64(9007199254740993), "neg": -2, "zero": 0,
		"f": 10.5, "_mission": "m-1", "_node": "n-1:undo", "_approval": map[string]any{"approved": true}}, "k-int")
	if err != nil || res["refunded"] != true {
		t.Fatalf("integer inputs must survive mint -> wire -> verify: %v %v", res, err)
	}
}

// A token signed with an EMPTY key must never verify, whatever the verifier's key (and no key = fail closed).
func TestForgedTokenUnderEmptyKeyIsRejected(t *testing.T) {
	inputs := map[string]any{"x": 1}
	payload := canonicalJSON(map[string]any{"operator": "o", "capability": "c", "mission_id": "m", "node_id": "n",
		"inputs_sha256": InputsHash(inputs), "marker": map[string]any{"approved": true}, "exp": int64(4102444800), "nonce": "00"})
	mac := hmac.New(sha256.New, []byte{})
	mac.Write(payload)
	forged := b64(payload) + "." + b64(mac.Sum(nil))
	if VerifyApprovalToken([]byte("real-key"), forged, "o", "c", inputs, 1, "m", "n", nil) != nil {
		t.Error("empty-key forgery verified under a real key")
	}
	t.Setenv(EnvApprovalSecret, "")
	if VerifyApprovalToken(nil, forged, "o", "c", inputs, 1, "m", "n", nil) != nil {
		t.Error("with NO key configured, an empty-key forgery must still be refused (fail closed)")
	}
	if VerifyApprovalToken([]byte{}, forged, "o", "c", inputs, 1, "m", "n", nil) != nil {
		t.Error("explicit empty secret must refuse")
	}
}

func TestReplayGuardFailsClosedWhenFullAndRecoversOnExpiry(t *testing.T) {
	g := NewReplayGuard(4)
	for i, n := range []string{"a", "b", "c", "d"} {
		if !g.Consume(n, 1000, 10) {
			t.Fatalf("nonce %d refused while there was room", i)
		}
	}
	if g.Consume("e", 1000, 10) {
		t.Error("a full table of unexpired nonces must refuse (fail closed), not evict")
	}
	if g.Consume("a", 1000, 10) {
		t.Error("replay must be refused")
	}
	if !g.Consume("e", 2000, 1001) { // all earlier nonces expired
		t.Error("expired entries must be purged to make room")
	}
}

func TestCompensatedMemoryIsBounded(t *testing.T) {
	old := compensatedCap
	compensatedCap = 3
	defer func() { compensatedCap = old }()
	c := NewInMemoryOperatorClient(map[string]Handler{
		"b.charge": func(map[string]any) (map[string]any, error) {
			return map[string]any{"charge_id": "c", "executed": true}, nil
		},
		"b.refund": func(map[string]any) (map[string]any, error) {
			return map[string]any{"refunded": true, "executed": true}, nil
		},
	})
	ex := NewExecutor(c)
	ex.EvidenceProbe = func(string, string) CompEvidence {
		return CompEvidence{Executed: true, Capability: "b.charge", Result: map[string]any{"charge_id": "c"}}
	}
	ex.UndoRegistry = func(string) string { return "b.refund" }
	for i := 0; i < 10; i++ {
		n := NewNode("b.charge", "b")
		n.ID, n.Undo, n.IdempotencyKey = "n"+strconv.Itoa(i), "b.refund", "k"+strconv.Itoa(i)
		ex.CompensateFor(&n, "m", 1, nil)
	}
	if len(ex.compensated) != 3 || len(ex.compOrder) != 3 {
		t.Fatalf("compensated memory must stay bounded: %d/%d", len(ex.compensated), len(ex.compOrder))
	}
}

// Wire-form idempotency: two distinct integers above 2^53 collapse to one float64 but are different requests;
// an out-of-range number is rejected with 400 (as before the wire-form change), not passed on as a string.
func TestOperatorDedupeUsesWireFormAndRejectsOutOfRangeNumbers(t *testing.T) {
	calls := 0
	op := NewOperator("x", []Capability{NewCapability("x.do", func(in map[string]any) (map[string]any, error) {
		calls++
		return map[string]any{"ok": true, "n": in["n"]}, nil
	}, SideEffecting())})
	srv := httptest.NewServer(op.HTTPHandler())
	defer srv.Close()
	post := func(body string) int {
		resp, err := http.Post(srv.URL+"/invoke", "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if post(`{"capability":"x.do","inputs":{"n":9007199254740993},"idempotency_key":"k"}`) != 200 ||
		post(`{"capability":"x.do","inputs":{"n":9007199254740992},"idempotency_key":"k"}`) != 200 || calls != 2 {
		t.Fatalf("distinct integers that collapse to one float64 must not share an idempotency key (calls=%d)", calls)
	}
	if post(`{"capability":"x.do","inputs":{"n":9007199254740993},"idempotency_key":"k"}`) != 200 || calls != 2 {
		t.Fatalf("the same wire request must still dedupe (calls=%d)", calls)
	}
	if code := post(`{"capability":"x.do","inputs":{"n":1e999},"idempotency_key":"k2"}`); code != 400 || calls != 2 {
		t.Fatalf("out-of-range number must be a 400 and never reach the handler: code=%d calls=%d", code, calls)
	}
}
