package mission

// Operator clients — how the Executor actually runs a capability. HTTPOperatorClient POSTs the
// owning app's /invoke (the capability-as-syscall REST surface). It implements OperatorClient, so
// the runtime is oblivious to which client it holds. Go port of agentic_os/mission/operators.py.

import (
	"fmt"
	"log"
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
	in := make(map[string]any, len(inputs))
	for k, v := range inputs {
		in[k] = v
	}
	marker, _ := in["_approval"].(map[string]any)
	delete(in, "_approval")
	nodeID, _ := in["_node"].(string)
	delete(in, "_node")
	var nodeAny, missionAny any
	if nodeID != "" {
		nodeAny = nodeID
	}
	mid := ""
	if v, ok := in["_mission"]; ok && v != nil {
		missionAny = v
		if s, isStr := v.(string); isStr {
			mid = s
		} else {
			mid = fmt.Sprint(v)
			missionAny = mid
		}
	}
	body := map[string]any{
		"capability": capability, "inputs": in,
		"mission_id": missionAny, "node_id": nodeAny,
		"idempotency_key": idempotencyKey,
	}
	if len(marker) > 0 {
		// The marker must not travel in `inputs` (the operator strips it as a forgery): send it as a signed token.
		// Never mint an UNBOUND-mission token (it would verify for any mission); with no secret nothing is sent and
		// the operator stages (fail closed).
		if strings.TrimSpace(mid) == "" {
			log.Printf("approval marker without _mission for %s/%s: not minting token (staged)", operator, capability)
		} else if tok := MintApprovalToken(nil, operator, capability, mid, nodeID, in, marker, 300); tok != "" {
			body["approval_token"] = tok
		} else {
			log.Printf("%s is not set: approved actions stay staged (no approval token minted)", EnvApprovalSecret)
		}
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
