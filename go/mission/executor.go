package mission

// Executor — runs ONE node durably by calling the owning operator's capability. In production a
// node becomes a Dagster op that POSTs the operator's /invoke; here an injectable OperatorClient
// runs it. Guarantees: exactly-once side effects (retries dedupe on idempotency_key) and sagas
// (a failed side-effecting node is compensated via its undo capability). Port of executor.py.

// OperatorError is a capability-execution failure (the Python OperatorError exception).
type OperatorError struct{ Msg string }

func (e *OperatorError) Error() string { return e.Msg }

// Handler is an in-process capability implementation: inputs → (result, error). A non-nil error
// simulates an operator failure (the Python handler raising).
type Handler func(inputs map[string]any) (map[string]any, error)

// OperatorClient is how the Executor runs a capability — in-process or over HTTP.
type OperatorClient interface {
	Invoke(operator, capability string, inputs map[string]any, idempotencyKey string) (map[string]any, error)
}

// InMemoryOperatorClient runs operators as in-process handlers (demo/tests), deduping on the
// idempotency key so a retried side-effecting call returns the first result instead of running twice.
type InMemoryOperatorClient struct {
	handlers map[string]Handler
	seen     map[string]map[string]any
	Calls    [][2]string // (capability, idempotency_key) — for assertions
}

func NewInMemoryOperatorClient(handlers map[string]Handler) *InMemoryOperatorClient {
	return &InMemoryOperatorClient{handlers: handlers, seen: map[string]map[string]any{}}
}

func (c *InMemoryOperatorClient) Invoke(operator, capability string, inputs map[string]any, idempotencyKey string) (map[string]any, error) {
	if idempotencyKey != "" {
		if r, ok := c.seen[idempotencyKey]; ok {
			return r, nil // exactly-once: return the prior result
		}
	}
	c.Calls = append(c.Calls, [2]string{capability, idempotencyKey})
	fn, ok := c.handlers[capability]
	if !ok {
		return nil, &OperatorError{Msg: "operator '" + operator + "' has no handler for '" + capability + "'"}
	}
	result, err := fn(inputs)
	if err != nil {
		return nil, err
	}
	if result == nil {
		result = map[string]any{}
	}
	if idempotencyKey != "" {
		c.seen[idempotencyKey] = result
	}
	return result, nil
}

// Executor runs a single node's capability, incrementing its attempt counter.
type Executor struct{ client OperatorClient }

func NewExecutor(client OperatorClient) *Executor { return &Executor{client: client} }

func (e *Executor) Run(node *Node, inputs map[string]any) (map[string]any, error) {
	node.Attempts++
	return e.client.Invoke(node.Operator, node.Capability, inputs, node.IdempotencyKey)
}

// Compensate runs the node's undo capability (saga) — best effort; never propagates the error.
func (e *Executor) Compensate(node *Node) map[string]any {
	if node.Undo == "" {
		return nil
	}
	res, err := e.client.Invoke(node.Operator, node.Undo,
		map[string]any{"undo_of": node.Capability, "result": node.Result},
		node.IdempotencyKey+":undo")
	if err != nil {
		return map[string]any{"undo_error": err.Error()}
	}
	return res
}
