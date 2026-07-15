package mission

// Operator clients — how the Executor actually runs a capability. HTTPOperatorClient POSTs the
// owning app's /invoke (the capability-as-syscall REST surface). It implements OperatorClient, so
// the runtime is oblivious to which client it holds. Go port of agentic_os/mission/operators.py.

import (
	"fmt"
	"strings"
)

// Resolver maps an operator name → its base URL (from a dict or modules.yaml in prod).
type Resolver func(operator string) string

// ResolverFromMap builds a Resolver from a static operator→baseURL map.
func ResolverFromMap(m map[string]string) Resolver {
	return func(operator string) string { return m[operator] }
}

// HTTPOperatorClient calls POST {operatorBase}/invoke with {capability, inputs, idempotency_key}.
// The idempotency key is also sent as an Idempotency-Key header so the operator can dedupe
// server-side (exactly-once for side effects). Returns the operator's result payload.
type HTTPOperatorClient struct {
	resolve   Resolver
	Timeout   float64
	Transport Transport
}

func NewHTTPOperatorClient(resolve Resolver) *HTTPOperatorClient {
	return &HTTPOperatorClient{resolve: resolve, Timeout: 60.0}
}

func (c *HTTPOperatorClient) base(operator string) (string, error) {
	b := c.resolve(operator)
	if b == "" {
		return "", fmt.Errorf("no base URL registered for operator %q", operator)
	}
	return strings.TrimRight(b, "/"), nil
}

func (c *HTTPOperatorClient) Invoke(operator, capability string, inputs map[string]any, idempotencyKey string) (map[string]any, error) {
	base, err := c.base(operator)
	if err != nil {
		return nil, err
	}
	body := map[string]any{
		"capability": capability, "inputs": inputs,
		"mission_id": inputs["_mission"], "node_id": nil,
		"idempotency_key": idempotencyKey,
	}
	headers := map[string]string{}
	if idempotencyKey != "" {
		headers["Idempotency-Key"] = idempotencyKey
	}
	data, err := postJSON(base+"/invoke", body, headers, c.Timeout, c.Transport)
	if err != nil {
		return nil, err
	}
	// tolerate either {"result": {...}} or a bare result object
	if res, ok := data["result"].(map[string]any); ok {
		return res, nil
	}
	return data, nil
}
