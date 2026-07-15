package mission

// P7B — the Verification Plane: acceptance control for consequential state transitions. The
// invariant: no consequential operator result becomes authoritative World State until the
// applicable acceptance policy succeeds. Pipeline: syntactic → policy → state-transition →
// ACCEPT / REJECT (hard) / ESCALATE (human decides). Go port of agentic_os/mission/verify.py.

import (
	"fmt"
	"math"
	"reflect"
)

// Verifier accepts, rejects or escalates a consequential result.
type Verifier interface {
	Verify(node *Node, result map[string]any, world *WorldState, mission *Mission) VerificationResult
}

// needsVerification: consequential = a side effect, or a node carrying explicit assertions.
func needsVerification(node *Node) bool {
	return node.SideEffecting || len(node.Assertions) > 0
}

// CompositeVerifier is the default acceptance pipeline. Hard failures (malformed result / lost
// permission) REJECT; unmet assertions ESCALATE; otherwise ACCEPT.
type CompositeVerifier struct {
	registry  Registry
	grantedOf func(*Mission) map[string]bool
}

func NewCompositeVerifier(registry Registry, grantedOf func(*Mission) map[string]bool) *CompositeVerifier {
	return &CompositeVerifier{registry: registry, grantedOf: grantedOf}
}

func (v *CompositeVerifier) Verify(node *Node, result map[string]any, world *WorldState, mission *Mission) VerificationResult {
	var checks []map[string]any
	cap := v.registry.Get(node.Capability)

	// 1. syntactic — the result carries the capability's declared outputs
	var outs []string
	if cap != nil {
		outs = mapKeys(cap.Outputs)
	}
	synOK := true
	for _, k := range outs {
		if _, ok := result[k]; !ok {
			synOK = false
			break
		}
	}
	checks = append(checks, map[string]any{"stage": "syntactic", "passed": synOK, "detail": fmt.Sprintf("expects outputs %v", outs)})

	// 2. policy — the capability's permissions still hold under the mission's grants
	granted := v.grantedOf(mission)
	polOK := granted["*"] || cap == nil
	if !polOK {
		polOK = true
		for _, p := range cap.Permissions {
			if !granted[p] {
				polOK = false
				break
			}
		}
	}
	checks = append(checks, map[string]any{"stage": "policy", "passed": polOK, "detail": "permissions still granted"})

	// 3. state-transition — every attached assertion holds against the result / world
	var unmet []string
	for i := range node.Assertions {
		a := &node.Assertions[i]
		holds := assertHolds(a, result, world)
		checks = append(checks, map[string]any{"stage": "assertion", "passed": holds,
			"detail": fmt.Sprintf("%s %s %v", a.Key, a.Op, a.Expected)})
		if !holds {
			unmet = append(unmet, a.Key)
		}
	}

	var decision VerificationDecision
	switch {
	case !synOK || !polOK:
		decision = VerReject
	case len(unmet) > 0:
		decision = VerEscalate
	default:
		decision = VerAccept
	}
	passed := 0
	for _, c := range checks {
		if c["passed"] == true {
			passed++
		}
	}
	conf := float64(passed) / math.Max(1, float64(len(checks)))
	vr := NewVerificationResult(decision)
	vr.Confidence = roundN(conf, 3)
	vr.Checks = checks
	return vr
}

func assertHolds(a *StateAssertion, result map[string]any, world *WorldState) bool {
	var val any
	if result != nil {
		if v, ok := result[a.Key]; ok {
			val = v
		} else if world != nil {
			val = world.Get(a.Key)
		}
	} else if world != nil {
		val = world.Get(a.Key)
	}
	a.Observed = val
	var holds bool
	switch a.Op {
	case "truthy":
		holds = truthy(val)
	case "equals":
		holds = reflect.DeepEqual(val, a.Expected)
	case "in":
		holds = inList(val, a.Expected)
	default: // present
		holds = val != nil
	}
	a.Holds = &holds
	return holds
}

func inList(val, expected any) bool {
	switch xs := expected.(type) {
	case []any:
		for _, x := range xs {
			if reflect.DeepEqual(x, val) {
				return true
			}
		}
	case []string:
		if s, ok := val.(string); ok {
			return strContains(xs, s)
		}
	}
	return false
}
