package mission

// Agent-assurance sensors over the mission event log (read-only). Go port of agentic_os/mission/assurance.py,
// asserted against conformance/vectors/assurance.json. Pure functions: they report what an operator, or a monitor such
// as Edge Sentinel, should look at BEFORE a customer does; they never change anything and never decide an action
// (an alert is not closure).
//
// Kinds: stuck_gate, dispatch_without_result (the effect MAY exist: reconcile before retrying), repeated_failures,
// approval_invalidated, approval_expired, unverified_success, ledger_tampered.

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
)

var assuranceSeverity = map[string]string{"ledger_tampered": "critical", "dispatch_without_result": "high",
	"approval_invalidated": "high", "unverified_success": "high", "repeated_failures": "medium", "stuck_gate": "medium",
	"approval_expired": "low"}

// AssuranceFinding is one thing to look at, with the event it points at as evidence.
type AssuranceFinding struct {
	Kind      string `json:"kind"`
	Severity  string `json:"severity"`
	MissionID string `json:"mission_id"`
	NodeID    string `json:"node_id"`
	Detail    string `json:"detail"`
	Seq       int    `json:"seq"`
}

// AssuranceOptions: thresholds. SideEffecting names capabilities whose success must have a recorded verification
// (nil = every NodeSucceeded is held to it).
type AssuranceOptions struct {
	GateAfterS     float64
	DispatchAfterS float64
	Failures       int
	WindowS        float64
	SideEffecting  map[string]bool
}

// DefaultAssuranceOptions mirrors the Python defaults.
func DefaultAssuranceOptions() AssuranceOptions {
	return AssuranceOptions{GateAfterS: 86_400, DispatchAfterS: 900, Failures: 3, WindowS: 3_600}
}

func strOf(v any) string {
	switch x := v.(type) {
	case nil:
		return ""
	case string:
		return x
	}
	return fmt.Sprint(v)
}

// numText renders a JSON number the way Python's str() shows what was written to the log.
func numText(v any) string {
	switch x := v.(type) {
	case nil:
		return "None"
	case json.Number:
		return x.String()
	}
	b, err := json.Marshal(v)
	if err != nil {
		return fmt.Sprint(v)
	}
	return string(b)
}

// AssuranceFindings returns the findings for events (any mix of missions), sorted by (seq, kind, node_id).
func AssuranceFindings(events []*Event, now float64, o AssuranceOptions) []AssuranceFinding {
	out := []AssuranceFinding{} // never nil: a clean log marshals as []
	add := func(kind string, ev *Event, node, detail string) {
		out = append(out, AssuranceFinding{kind, assuranceSeverity[kind], ev.MissionID, node, detail, ev.Seq})
	}
	if rep := VerifyChain(events); !rep.OK {
		var bad *Event
		for _, e := range events {
			if e.Seq == rep.FirstBadSeq {
				bad = e
				break
			}
		}
		if bad == nil && len(events) > 0 {
			bad = events[0]
		}
		if bad != nil {
			add("ledger_tampered", bad, "", rep.Reason)
		}
	}
	var order []string
	byMission := map[string][]*Event{}
	for _, e := range events {
		if _, ok := byMission[e.MissionID]; !ok {
			order = append(order, e.MissionID)
		}
		byMission[e.MissionID] = append(byMission[e.MissionID], e)
	}
	for _, mid := range order {
		parked, dispatched := map[string]*Event{}, map[string]*Event{}
		verifiedAfter := map[string]bool{}
		failedAt := map[string][]*Event{}
		var failedOrder []string
		for _, e := range byMission[mid] {
			node := strOf(e.Payload["node_id"])
			switch e.Type {
			case "NodeParked":
				parked[node] = e
			case "ApprovalGranted", "ApprovalRejected", "NodeResumed", "NodeSkipped":
				delete(parked, node)
			case "NodeDispatched":
				dispatched[node] = e
			case "NodeSucceeded", "NodeFailed":
				delete(dispatched, node)
				capRaw, hasCap := e.Payload["capability"]
				capName := node
				if hasCap {
					capName = strOf(capRaw)
				}
				if e.Type == "NodeFailed" {
					if _, ok := failedAt[capName]; !ok {
						failedOrder = append(failedOrder, capName)
					}
					failedAt[capName] = append(failedAt[capName], e)
				} else if o.SideEffecting == nil || (hasCap && o.SideEffecting[strOf(capRaw)]) {
					if !verifiedAfter[node] {
						add("unverified_success", e, node, capName+" succeeded with no recorded verification before it")
					}
				}
			case "VerificationRecorded":
				verifiedAfter[node] = true
			case "ApprovalInvalidated":
				add("approval_invalidated", e, node, strOf(e.Payload["reason"]))
			case "ApprovalExpired":
				add("approval_expired", e, node, "gate lapsed after "+numText(e.Payload["ttl_s"])+"s")
			}
		}
		for node, e := range parked {
			if now-e.Ts > o.GateAfterS {
				add("stuck_gate", e, node, fmt.Sprintf("waiting for a human for %d min", int(math.Floor((now-e.Ts)/60))))
			}
		}
		for node, e := range dispatched {
			if now-e.Ts > o.DispatchAfterS {
				add("dispatch_without_result", e, node,
					"dispatched with no result: the effect may exist; reconcile before retrying")
			}
		}
		for _, capName := range failedOrder {
			var recent []*Event
			for _, f := range failedAt[capName] {
				if now-f.Ts <= o.WindowS {
					recent = append(recent, f)
				}
			}
			if len(recent) >= o.Failures {
				last := recent[len(recent)-1]
				add("repeated_failures", last, strOf(last.Payload["node_id"]),
					fmt.Sprintf("%s failed %d times in %d min", capName, len(recent), int(math.Floor(o.WindowS/60))))
			}
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.Seq != b.Seq {
			return a.Seq < b.Seq
		}
		if a.Kind != b.Kind {
			return a.Kind < b.Kind
		}
		return a.NodeID < b.NodeID
	})
	return out
}
