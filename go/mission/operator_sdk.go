package mission

// Operator SDK — how an agentic app becomes a capability operator (a v5 "process"). An app declares
// its capabilities once and gets GET /capabilities (the manifest the planner discovers) + POST
// /invoke (run a capability, server-side idempotency dedupe) + a LocalOperatorClient for co-located
// operators. This is the seam that turns agents/<app> into a Mission Runtime operator without
// changing how it runs standalone. Go port of agentic_os/mission/operator_sdk.py.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
)

// Capability is a capability spec bound to its handler.
type Capability struct {
	Spec    CapabilitySpec
	Handler Handler
}

// CapOption configures a capability spec (the ergonomic builder args).
type CapOption func(*CapabilitySpec)

func WithProvides(p ...string) CapOption       { return func(s *CapabilitySpec) { s.Provides = p } }
func WithPermissions(p ...string) CapOption    { return func(s *CapabilitySpec) { s.Permissions = p } }
func WithInputs(m map[string]string) CapOption { return func(s *CapabilitySpec) { s.Inputs = m } }
func WithOutputs(m map[string]string) CapOption {
	return func(s *CapabilitySpec) { s.Outputs = m }
}
func SideEffecting() CapOption    { return func(s *CapabilitySpec) { s.SideEffecting = true } }
func ApprovalRequired() CapOption { return func(s *CapabilitySpec) { s.ApprovalRequired = true } }
func WithUndo(u string) CapOption { return func(s *CapabilitySpec) { s.Undo = u } }

// WithCompensation declares the capability's compensation contract (docs/COMPENSATION.md).
func WithCompensation(c CompensationSpec) CapOption {
	return func(s *CapabilitySpec) { cc := c; s.Compensation = &cc }
}

// WithIrreversible declares the capability irreversible, with the required note (why it cannot be undone).
func WithIrreversible(note string) CapOption {
	return func(s *CapabilitySpec) { s.Irreversible, s.IrreversibleNote = true, note }
}

// AsMissionOwnedTemporary marks the owner-side UNDO of a temporary artifact (in-process only; never read from JSON).
func AsMissionOwnedTemporary() CapOption {
	return func(s *CapabilitySpec) { s.MissionOwnedTemporary = true }
}
func WithValue(v string) CapOption    { return func(s *CapabilitySpec) { s.EstimatedValue = v } }
func WithOperator(o string) CapOption { return func(s *CapabilitySpec) { s.Operator = o } }
func WithConcurrencyMode(m string) CapOption {
	return func(s *CapabilitySpec) { s.ConcurrencyMode = m }
}
func WithConcurrencyKey(k string) CapOption  { return func(s *CapabilitySpec) { s.ConcurrencyKey = k } }
func WithResourceKeys(k ...string) CapOption { return func(s *CapabilitySpec) { s.ResourceKeys = k } }
func WithMaxParallelism(n int) CapOption     { return func(s *CapabilitySpec) { s.MaxParallelism = n } }
func WithCost(usd float64, latencyMs int) CapOption {
	return func(s *CapabilitySpec) { s.Cost = NodeCost{USD: usd, LatencyMs: latencyMs} }
}

// NewCapability is the ergonomic builder for one capability + its handler.
func NewCapability(name string, handler Handler, opts ...CapOption) Capability {
	spec := NewCapabilitySpec(name, "")
	for _, o := range opts {
		o(&spec)
	}
	return Capability{Spec: spec, Handler: handler}
}

// Operator is one app's capability operator: manifest + handlers + idempotency dedupe.
type Operator struct {
	Name     string
	Manifest CapabilityManifest
	handlers map[string]Handler
	specs    map[string]CapabilitySpec
	seen     map[string]map[string]any
	// ReplayGuard makes a verified approval token single-use (per process; multi-replica needs a shared store).
	ReplayGuard *ReplayGuard
	Calls       [][2]string // (capability, idempotency_key) — for assertions
	mu          sync.Mutex  // guards seen + Calls
}

// CallsSnapshot returns a copy of the recorded calls, safe to read while Invoke runs concurrently.
func (o *Operator) CallsSnapshot() [][2]string {
	o.mu.Lock()
	defer o.mu.Unlock()
	return append([][2]string(nil), o.Calls...)
}

func NewOperator(name string, caps []Capability) *Operator {
	specs := make([]CapabilitySpec, 0, len(caps))
	handlers := make(map[string]Handler, len(caps))
	byName := make(map[string]CapabilitySpec, len(caps))
	for i := range caps {
		if caps[i].Spec.Operator == "" {
			caps[i].Spec.Operator = name
		}
		specs = append(specs, caps[i].Spec)
		handlers[caps[i].Spec.Name] = caps[i].Handler
		byName[caps[i].Spec.Name] = caps[i].Spec
	}
	return &Operator{
		Name: name, Manifest: CapabilityManifest{Operator: name, Capabilities: specs},
		handlers: handlers, specs: byName, seen: map[string]map[string]any{}, ReplayGuard: NewReplayGuard(0),
	}
}

// dedupeKey: (capability, canonical inputs minus runtime meta, idempotency key) - the same key with a different
// capability or inputs is a different call. `_approval` stays IN the hash so an approved retry is never served the
// staged result. "" when there is no key.
func dedupeKey(capability string, inputs map[string]any, key string) string {
	if key == "" {
		return ""
	}
	clean := map[string]any{}
	for k, v := range inputs {
		if k != "_mission" && k != "_goal" && k != "_node" {
			clean[k] = v
		}
	}
	return capability + "\x00" + string(canonicalJSON(clean)) + "\x00" + key
}

func (o *Operator) Invoke(capability string, inputs map[string]any, idempotencyKey string) (map[string]any, error) {
	return o.invokeKeyed(capability, inputs, idempotencyKey, dedupeKey(capability, inputs, idempotencyKey))
}

// invokeKeyed runs a capability under a precomputed dedupe key (the HTTP path derives it from the WIRE form of the
// inputs, so two distinct integers that collapse to one float64 never share a key).
func (o *Operator) invokeKeyed(capability string, inputs map[string]any, idempotencyKey, dk string) (map[string]any, error) {
	if dk != "" {
		o.mu.Lock()
		r, ok := o.seen[dk]
		o.mu.Unlock()
		if ok {
			return r, nil
		}
	}
	fn, ok := o.handlers[capability]
	if !ok {
		return nil, fmt.Errorf("operator %q has no capability %q", o.Name, capability)
	}
	o.mu.Lock()
	o.Calls = append(o.Calls, [2]string{capability, idempotencyKey})
	o.mu.Unlock()
	result, err := fn(inputs)
	if err != nil {
		return nil, err
	}
	if result == nil {
		result = map[string]any{}
	}
	// Cache ONLY successes (a failed / empty-write result must not pin the key, so a retry after the backend
	// recovers re-executes); a staged pending_approval result is side-effect-free and is cached.
	spec := o.specs[capability]
	failed, _ := ResultFailed(result, spec.SideEffecting, len(spec.Outputs) > 0)
	if dk != "" && (!failed || IsStaged(result)) {
		o.mu.Lock()
		o.seen[dk] = result
		o.mu.Unlock()
	}
	return result, nil
}

// Handler returns the operator's HTTP surface (mount into the app's server): GET /capabilities and
// POST /invoke with server-side idempotency (also honouring the Idempotency-Key header).
func (o *Operator) HTTPHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /capabilities", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, jsonable(o.Manifest))
	})
	mux.HandleFunc("POST /invoke", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Capability     string         `json:"capability"`
			Inputs         map[string]any `json:"inputs"`
			IdempotencyKey string         `json:"idempotency_key"`
			MissionID      string         `json:"mission_id"`
			NodeID         string         `json:"node_id"`
			ApprovalToken  string         `json:"approval_token"`
		}
		dec := json.NewDecoder(r.Body)
		dec.UseNumber() // keep number tokens verbatim: the approval token binds the inputs hash computed from the wire form
		if err := dec.Decode(&body); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"detail": err.Error()})
			return
		}
		key := body.IdempotencyKey
		if key == "" {
			key = r.Header.Get("Idempotency-Key")
		}
		// The HTTP /invoke is the UNTRUSTED "try" surface: strip every runtime-only approval marker a caller could
		// forge. A runtime-delivered approval arrives ONLY as a signed, operator/capability/inputs/mission/node-bound,
		// single-use token; verified => the marker is injected AFTER the strip, anything else => ignored (stages).
		inputs, _ := StripApprovalKeys(body.Inputs).(map[string]any)
		if inputs == nil {
			inputs = map[string]any{}
		}
		if body.ApprovalToken != "" {
			if marker := VerifyApprovalToken(nil, body.ApprovalToken, o.Name, body.Capability, inputs, 0,
				body.MissionID, body.NodeID, o.ReplayGuard); marker != nil {
				inputs["_approval"] = marker
			}
		}
		// handlers see ordinary JSON numbers (float64), as before; the idempotency key uses the wire form
		handlerIn, nerr := numbersToFloatCopy(inputs)
		if nerr != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"detail": nerr.Error()}) // e.g. 1e999: out of range, as before
			return
		}
		res, err := o.invokeKeyed(body.Capability, handlerIn.(map[string]any), key, dedupeKey(body.Capability, inputs, key))
		if err != nil {
			writeJSON(w, http.StatusNotFound, map[string]any{"detail": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"result": res})
	})
	return mux
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

// LocalOperatorClient drives co-located in-process Operators (no HTTP hop). Remote apps use
// HTTPOperatorClient instead.
type LocalOperatorClient struct{ ops map[string]*Operator }

func NewLocalOperatorClient(ops map[string]*Operator) *LocalOperatorClient {
	return &LocalOperatorClient{ops: ops}
}

func (c *LocalOperatorClient) Invoke(operator, capability string, inputs map[string]any, idempotencyKey string) (map[string]any, error) {
	op, ok := c.ops[operator]
	if !ok {
		return nil, fmt.Errorf("no operator %q registered", operator)
	}
	return op.Invoke(capability, inputs, idempotencyKey)
}

// numbersToFloatCopy returns a copy of a decoded body with every json.Number converted to float64 (the form handlers
// have always received); a number that does not fit a float64 is an error (the request is rejected, as before).
func numbersToFloatCopy(v any) (any, error) {
	switch x := v.(type) {
	case json.Number:
		f, err := x.Float64()
		if err != nil {
			return nil, fmt.Errorf("number %s out of range", string(x))
		}
		return f, nil
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			c, err := numbersToFloatCopy(e)
			if err != nil {
				return nil, err
			}
			out[k] = c
		}
		return out, nil
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			c, err := numbersToFloatCopy(e)
			if err != nil {
				return nil, err
			}
			out[i] = c
		}
		return out, nil
	}
	return v, nil
}
