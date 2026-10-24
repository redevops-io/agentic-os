package mission

import (
	"fmt"
	"strconv"
	"strings"
	"sync"
)

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
	mu       sync.Mutex  // guards seen + Calls so the client is safe under a parallel wave
}

func NewInMemoryOperatorClient(handlers map[string]Handler) *InMemoryOperatorClient {
	return &InMemoryOperatorClient{handlers: handlers, seen: map[string]map[string]any{}}
}

// CallsSnapshot returns a copy of the recorded calls, safe to read while Invoke runs concurrently.
func (c *InMemoryOperatorClient) CallsSnapshot() [][2]string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([][2]string(nil), c.Calls...)
}

func (c *InMemoryOperatorClient) Invoke(operator, capability string, inputs map[string]any, idempotencyKey string) (map[string]any, error) {
	if idempotencyKey != "" {
		c.mu.Lock()
		r, ok := c.seen[idempotencyKey]
		c.mu.Unlock()
		if ok {
			return r, nil // exactly-once: return the prior result
		}
	}
	c.mu.Lock()
	c.Calls = append(c.Calls, [2]string{capability, idempotencyKey})
	c.mu.Unlock()
	fn, ok := c.handlers[capability] // handlers is read-only after construction
	if !ok {
		return nil, &OperatorError{Msg: "operator '" + operator + "' has no handler for '" + capability + "'"}
	}
	result, err := fn(inputs) // NOT under lock — this is the parallelizable work
	if err != nil {
		return nil, err
	}
	if result == nil {
		result = map[string]any{}
	}
	if idempotencyKey != "" {
		c.mu.Lock()
		c.seen[idempotencyKey] = result
		c.mu.Unlock()
	}
	return result, nil
}

// CompEvidence is compensation authorization evidence folded from the durable event log.
type CompEvidence struct {
	Executed          bool
	Capability        string
	Approved          bool
	Result            any
	CompensatedResult map[string]any
}

// Executor runs a single node's capability, incrementing its attempt counter.
type Executor struct {
	client OperatorClient
	// Compensation authorization seams, wired by the runtime from the DURABLE event log and the capability
	// registry (never from plan fields): EvidenceProbe(mission, node) -> did THIS node execute in THIS mission;
	// UndoRegistry(capability) -> the trusted registered undo; CompRequiresApproval(capability) -> needs its own
	// human approval. With no probe wired, compensation is refused (fail closed).
	EvidenceProbe        func(missionID, nodeID string) CompEvidence
	UndoRegistry         func(capability string) string
	CompRequiresApproval func(capability string) bool

	mu          sync.Mutex
	compensated map[string]map[string]any
	compOrder   []string // insertion order of `compensated`, so it stays bounded (oldest evicted first)
}

// compensatedCap bounds the per-executor idempotency memory of finished compensations (the durable log is the
// authority; this only avoids a second undo call within a process). Var so tests can shrink it.
var compensatedCap = 4096

func NewExecutor(client OperatorClient) *Executor {
	return &Executor{client: client, compensated: map[string]map[string]any{}}
}

func (e *Executor) Run(node *Node, inputs map[string]any) (map[string]any, error) {
	node.Attempts++
	return e.client.Invoke(node.Operator, node.Capability, inputs, node.IdempotencyKey)
}

// skip: a visible, non-executing outcome. `undo_error` makes the runner record the failure and keep the effect.
func (e *Executor) skip(node *Node, reason string) map[string]any {
	return map[string]any{"compensation_skipped": true, "status": "compensation_skipped", "executed": false,
		"undo_error": "compensation skipped for '" + node.Capability + "': " + reason}
}

// Compensate is the legacy entry point (no mission id): it can never prove the forward step ran, so it refuses.
func (e *Executor) Compensate(node *Node) map[string]any {
	if node.Undo == "" {
		return nil
	}
	return e.skip(node, "forward_not_executed: no mission id")
}

// CompensateFor runs the node's undo capability (saga) - best effort, never raises. Fails closed
// (docs/SECURITY_MODEL.md, "Compensation authorization"): the undo is invoked only if THIS node (mission + node id +
// capability) is shown to have EXECUTED in THIS mission, the undo equals the registry-declared undo, the target is
// not the forward action, and - when the registry declares requires_approval - `approval` (a CompensationApproved
// record) matches this mission, node, attempt, undo and the exact input hash and is unexpired. The undo's inputs
// come from the logged forward result. Compensating twice returns the first outcome without a second undo call.
// Dispatch is scoped to the compensation: idempotency key `{key}:undo:{attempt}`, `_node` = `{node.id}:undo`.
func (e *Executor) CompensateFor(node *Node, missionID string, attempt int, approval map[string]any) map[string]any {
	if node.Undo == "" {
		return nil
	}
	mid := strings.TrimSpace(missionID)
	if mid == "" {
		return e.skip(node, "forward_not_executed: no mission id")
	}
	if e.EvidenceProbe == nil {
		return e.skip(node, "forward_not_executed: evidence unavailable: no evidence probe wired")
	}
	ev := e.EvidenceProbe(mid, node.ID)
	if !ev.Executed {
		return e.skip(node, fmt.Sprintf("forward_not_executed: no record that node %s executed in mission %s", node.ID, mid))
	}
	if ev.Capability == "" || ev.Capability != node.Capability {
		return e.skip(node, fmt.Sprintf("forward_not_executed: node %s executed capability '%s', not '%s'", node.ID, ev.Capability, node.Capability))
	}
	key := mid + "\x00" + node.ID + "\x00" + node.Capability + "\x00" + node.IdempotencyKey
	prior := ev.CompensatedResult
	if prior == nil {
		e.mu.Lock()
		prior = e.compensated[key]
		e.mu.Unlock()
	}
	if prior != nil {
		return prior // idempotent: already compensated, no second undo call
	}
	if node.Undo == node.Capability {
		return e.skip(node, fmt.Sprintf("compensation_target_is_forward_action: '%s' is the forward capability itself (would re-run it under the compensation marker)", node.Undo))
	}
	registered := ""
	if e.UndoRegistry != nil {
		registered = e.UndoRegistry(node.Capability)
	}
	if e.UndoRegistry != nil && registered != node.Undo {
		return e.skip(node, fmt.Sprintf("undo_not_registered: '%s' is not the registered undo of '%s'", node.Undo, node.Capability))
	}
	inputs := undoInputs(node, mid, ev.Result)
	needsOwn := e.CompRequiresApproval != nil && e.CompRequiresApproval(node.Capability)
	if needsOwn {
		a := approval
		ok := a != nil && payloadStr(a, "mission_id") == mid && payloadStr(a, "node_id") == node.ID &&
			payloadStr(a, "undo") == node.Undo && payloadInt(a, "attempt") == attempt &&
			payloadStr(a, "inputs_sha256") == InputsHash(inputs) && toFloat(a["exp"], 0) > now()
		if !ok {
			return e.skip(node, fmt.Sprintf("compensation_requires_approval: '%s' needs its OWN human approval for this attempt, undo and exact inputs (missing, expired, or bound to different inputs/mission/attempt)", node.Undo))
		}
	}
	if e.UndoRegistry != nil {
		// Trusted registry declares this undo and the forward step demonstrably executed: the undo carries the
		// compensation marker (+ own_approval when it required, and has, its own human approval).
		marker := map[string]any{"approved": true, "compensation": true}
		if needsOwn {
			marker["own_approval"] = true
		}
		inputs["_approval"] = marker
	}
	out, err := e.client.Invoke(node.Operator, node.Undo, inputs, node.IdempotencyKey+":undo:"+strconv.Itoa(attempt))
	if err != nil {
		return map[string]any{"undo_error": err.Error()}
	}
	if failed, _ := ResultFailed(out, false, true); out != nil && out["undo_error"] == nil && !IsStaged(out) && !failed {
		e.mu.Lock()
		if _, seen := e.compensated[key]; !seen {
			e.compOrder = append(e.compOrder, key)
		}
		e.compensated[key] = out
		for len(e.compOrder) > compensatedCap {
			delete(e.compensated, e.compOrder[0])
			e.compOrder = e.compOrder[1:]
		}
		e.mu.Unlock()
	}
	return out
}
