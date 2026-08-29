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
func SideEffecting() CapOption        { return func(s *CapabilitySpec) { s.SideEffecting = true } }
func ApprovalRequired() CapOption     { return func(s *CapabilitySpec) { s.ApprovalRequired = true } }
func WithUndo(u string) CapOption     { return func(s *CapabilitySpec) { s.Undo = u } }
func WithValue(v string) CapOption    { return func(s *CapabilitySpec) { s.EstimatedValue = v } }
func WithOperator(o string) CapOption { return func(s *CapabilitySpec) { s.Operator = o } }
func WithConcurrencyMode(m string) CapOption { return func(s *CapabilitySpec) { s.ConcurrencyMode = m } }
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
	seen     map[string]map[string]any
	Calls    [][2]string // (capability, idempotency_key) — for assertions
}

func NewOperator(name string, caps []Capability) *Operator {
	specs := make([]CapabilitySpec, 0, len(caps))
	handlers := make(map[string]Handler, len(caps))
	for i := range caps {
		if caps[i].Spec.Operator == "" {
			caps[i].Spec.Operator = name
		}
		specs = append(specs, caps[i].Spec)
		handlers[caps[i].Spec.Name] = caps[i].Handler
	}
	return &Operator{
		Name: name, Manifest: CapabilityManifest{Operator: name, Capabilities: specs},
		handlers: handlers, seen: map[string]map[string]any{},
	}
}

func (o *Operator) Invoke(capability string, inputs map[string]any, idempotencyKey string) (map[string]any, error) {
	if idempotencyKey != "" {
		if r, ok := o.seen[idempotencyKey]; ok {
			return r, nil
		}
	}
	fn, ok := o.handlers[capability]
	if !ok {
		return nil, fmt.Errorf("operator %q has no capability %q", o.Name, capability)
	}
	o.Calls = append(o.Calls, [2]string{capability, idempotencyKey})
	result, err := fn(inputs)
	if err != nil {
		return nil, err
	}
	if result == nil {
		result = map[string]any{}
	}
	if idempotencyKey != "" {
		o.seen[idempotencyKey] = result
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
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"detail": err.Error()})
			return
		}
		key := body.IdempotencyKey
		if key == "" {
			key = r.Header.Get("Idempotency-Key")
		}
		res, err := o.Invoke(body.Capability, body.Inputs, key)
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
