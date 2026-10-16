package mission

import (
	"net/http/httptest"
	"testing"
)

func TestInMemoryClientDedupesOnIdempotencyKey(t *testing.T) {
	n := 0
	c := NewInMemoryOperatorClient(map[string]Handler{
		"x.do": func(map[string]any) (map[string]any, error) { n++; return map[string]any{"n": n}, nil },
	})
	r1, _ := c.Invoke("x", "x.do", nil, "k1")
	r2, _ := c.Invoke("x", "x.do", nil, "k1") // retry, same key
	if r1["n"] != 1.0 && r1["n"] != 1 {       // (native int from handler)
		t.Fatalf("first = %v", r1)
	}
	if n != 1 || r2["n"] != r1["n"] {
		t.Fatalf("idempotency broken: n=%d r2=%v", n, r2)
	}
}

func TestExecutorRunAndCompensate(t *testing.T) {
	c := NewInMemoryOperatorClient(map[string]Handler{
		"b.charge": func(map[string]any) (map[string]any, error) { return map[string]any{"charge_id": "ch1"}, nil },
		"b.refund": func(in map[string]any) (map[string]any, error) { return map[string]any{"refunded": in["undo_of"]}, nil },
	})
	ex := NewExecutor(c)
	node := NewNode("b.charge", "b")
	node.Undo = "b.refund"
	node.IdempotencyKey = "idem1"

	res, err := ex.Run(&node, map[string]any{})
	if err != nil || res["charge_id"] != "ch1" || node.Attempts != 1 {
		t.Fatalf("run = %v err=%v attempts=%d", res, err, node.Attempts)
	}
	// fail closed: no mission / no evidence the forward step ran -> the undo is NOT invoked
	if comp := ex.Compensate(&node); comp["compensation_skipped"] != true || len(c.CallsSnapshot()) != 1 {
		t.Fatalf("legacy Compensate must refuse: %v", comp)
	}
	ex.EvidenceProbe = func(string, string) CompEvidence {
		return CompEvidence{Executed: true, Capability: "b.charge", Result: map[string]any{"charge_id": "ch1"}}
	}
	ex.UndoRegistry = func(string) string { return "b.refund" }
	comp := ex.CompensateFor(&node, "m1", 1, nil)
	if comp["refunded"] != "b.charge" {
		t.Fatalf("compensate = %v", comp)
	}
	if last := c.CallsSnapshot()[1]; last[1] != "idem1:undo:1" {
		t.Fatalf("attempt-scoped idempotency key expected, got %v", last)
	}
	if again := ex.CompensateFor(&node, "m1", 2, nil); again["refunded"] != "b.charge" || len(c.CallsSnapshot()) != 2 {
		t.Fatalf("a second compensation must return the first outcome without a second undo call: %v", again)
	}
	// a node with no undo compensates to nil
	if ex.Compensate(&Node{Operator: "b", Capability: "b.charge"}) != nil {
		t.Fatal("no-undo node should compensate to nil")
	}
}

func opFixture() *Operator {
	return NewOperator("billing", []Capability{
		NewCapability("billing.create_subscription",
			func(in map[string]any) (map[string]any, error) {
				return map[string]any{"subscription_id": "sub_1", "plan": in["plan"]}, nil
			},
			WithProvides("subscription"), SideEffecting(), WithValue("high")),
	})
}

func TestLocalOperatorClient(t *testing.T) {
	c := NewLocalOperatorClient(map[string]*Operator{"billing": opFixture()})
	res, err := c.Invoke("billing", "billing.create_subscription", map[string]any{"plan": "pro"}, "k")
	if err != nil || res["subscription_id"] != "sub_1" || res["plan"] != "pro" {
		t.Fatalf("local invoke = %v err=%v", res, err)
	}
	if _, err := c.Invoke("nope", "x", nil, ""); err == nil {
		t.Fatal("unknown operator should error")
	}
}

// The language-agnostic proof: drive an Operator over the real HTTP /invoke surface via
// HTTPOperatorClient — the exact path a Go mission-runtime uses to reach a (Python) app.
func TestHTTPOperatorClientOverInvokePath(t *testing.T) {
	srv := httptest.NewServer(opFixture().HTTPHandler())
	defer srv.Close()

	client := NewHTTPOperatorClient(ResolverFromMap(map[string]string{"billing": srv.URL}))
	res, err := client.Invoke("billing", "billing.create_subscription",
		map[string]any{"plan": "pro", "_mission": "m1"}, "idem-http")
	if err != nil {
		t.Fatalf("http invoke error: %v", err)
	}
	if res["subscription_id"] != "sub_1" || res["plan"] != "pro" {
		t.Fatalf("http invoke result = %v", res)
	}
	// unknown operator base → error
	if _, err := client.Invoke("ghost", "x", nil, ""); err == nil {
		t.Fatal("unresolved operator should error")
	}
}
