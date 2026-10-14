package mission

// Fork/join event folding — Go port of the pure fold in agentic_os/mission/fork_join.py
// (docs/MISSION_FORK_JOIN.md). This is replay-only parity: a Go runtime can fold, display and replay a
// mission that contains forks, but it does not yet OPEN or DRIVE forks (controller, scheduler, budget,
// approve hook, subscriptions are deferred). Existing folds already ignore the Fork/Branch/Join event
// types, so adding them to a log never breaks Go replay.

import "strings"

// BranchState is one branch's folded state.
type BranchState struct {
	BranchID       string
	NodeID         string
	Capability     string
	Status         string // pending|running|parked|retrying|succeeded|failed|cancelled
	Attempt        int
	IdempotencyKey string
	Reason         string
	Ran            bool
	Result         any
	Receipt        any
	Approval       *string
	Parked         bool
}

// ForkState is the folded state of one fork.
type ForkState struct {
	ForkID       string
	Opened       bool
	SpecDigest   string
	Join         map[string]any
	Order        []string
	Branches     map[string]*BranchState
	SuccessOrder []string
	LateResults  []map[string]any
	Closed       string // "" | "satisfied" | "failed"
	ClosePayload map[string]any
}

func str(p map[string]any, k string) string {
	if v, ok := p[k].(string); ok {
		return v
	}
	return ""
}

func isTerminalBranch(s string) bool { return s == "succeeded" || s == "failed" || s == "cancelled" }

// FoldFork is the pure fold of a mission's events for one fork: same log -> same state.
func FoldFork(events []*Event, forkID string) *ForkState {
	st := &ForkState{ForkID: forkID, Branches: map[string]*BranchState{}, Join: map[string]any{},
		ClosePayload: map[string]any{}}
	byNode := map[string]string{}
	for _, ev := range events {
		t, p := ev.Type, ev.Payload
		if p == nil {
			p = map[string]any{}
		}
		if t == "ForkOpened" && str(p, "fork_id") == forkID && !st.Opened {
			st.Opened = true
			st.SpecDigest = str(p, "spec_digest")
			if j, ok := p["join"].(map[string]any); ok {
				st.Join = j
			}
			if bl, ok := p["branches"].([]any); ok {
				for _, raw := range bl {
					b, _ := raw.(map[string]any)
					bs := &BranchState{BranchID: str(b, "branch_id"), NodeID: str(b, "node_id"),
						Capability: str(b, "capability"), Status: "pending"}
					st.Order = append(st.Order, bs.BranchID)
					st.Branches[bs.BranchID] = bs
					byNode[bs.NodeID] = bs.BranchID
				}
			}
			continue
		}
		if !st.Opened {
			continue
		}
		switch t {
		case "NodeParked", "NodeResumed", "NodeSkipped", "ApprovalGranted", "ApprovalInvalidated", "ApprovalRejected":
			bid, ok := byNode[str(p, "node_id")]
			if !ok {
				continue
			}
			b := st.Branches[bid]
			switch t {
			case "NodeParked":
				b.Parked = true
			case "NodeResumed", "NodeSkipped":
				b.Parked = false
			case "ApprovalGranted":
				c := str(p, "capability")
				b.Approval = &c
			case "ApprovalInvalidated":
				b.Approval = nil
			}
			continue
		}
		if str(p, "fork_id") != forkID || !strings.HasPrefix(t, "Branch") && t != "JoinSatisfied" && t != "JoinFailed" {
			continue
		}
		b := st.Branches[str(p, "branch_id")]
		closed := st.Closed != ""
		attempt := 0
		if f, ok := p["attempt"].(float64); ok {
			attempt = int(f)
		} else if i, ok := p["attempt"].(int); ok {
			attempt = i
		}
		switch t {
		case "BranchStarted":
			if b == nil || isTerminalBranch(b.Status) || closed {
				continue
			}
			if attempt == 0 {
				attempt = 1
			}
			b.Status, b.Attempt, b.IdempotencyKey, b.Ran = "running", attempt, str(p, "idempotency_key"), true
		case "BranchSucceeded":
			if b == nil {
				continue
			}
			if late, _ := p["late"].(bool); late || isTerminalBranch(b.Status) || closed {
				st.LateResults = append(st.LateResults, map[string]any{"branch_id": b.BranchID,
					"attempt": p["attempt"], "receipt": p["receipt"], "result": p["result"]})
				continue
			}
			b.Status, b.Result, b.Receipt, b.Reason = "succeeded", p["result"], p["receipt"], ""
			st.SuccessOrder = append(st.SuccessOrder, b.BranchID)
		case "BranchFailed":
			if b == nil || isTerminalBranch(b.Status) || closed {
				continue
			}
			b.Reason = str(p, "reason")
			b.Ran, _ = p["ran"].(bool)
			b.Result, b.Receipt = p["result"], p["receipt"]
			if retry, _ := p["will_retry"].(bool); retry {
				b.Status = "retrying"
			} else {
				b.Status = "failed"
			}
		case "BranchCancelled":
			if b == nil || isTerminalBranch(b.Status) || closed {
				continue
			}
			b.Status, b.Reason = "cancelled", str(p, "reason")
		case "JoinSatisfied":
			if !closed {
				st.Closed, st.ClosePayload = "satisfied", p
			}
		case "JoinFailed":
			if !closed {
				st.Closed, st.ClosePayload = "failed", p
			}
		}
	}
	for _, b := range st.Branches {
		if b.Parked && (b.Status == "pending" || b.Status == "retrying") {
			b.Status = "parked"
		}
	}
	return st
}

// ForkIDs lists fork ids opened in the log, in log order.
func ForkIDs(events []*Event) []string {
	var out []string
	seen := map[string]bool{}
	for _, ev := range events {
		if ev.Type == "ForkOpened" {
			if id := str(ev.Payload, "fork_id"); !seen[id] {
				seen[id] = true
				out = append(out, id)
			}
		}
	}
	return out
}

// EvaluateJoin is the pure join verdict: "satisfied" | "failed" | "pending".
func EvaluateJoin(mode string, k, n, s, f, c int) string {
	r := n - s - f - c
	if mode == "all" {
		if s == n {
			return "satisfied"
		}
		if f+c > 0 {
			return "failed"
		}
		return "pending"
	}
	need := 1
	if mode != "any" {
		need = k
		if need < 1 {
			need = 1
		}
	}
	if s >= need {
		return "satisfied"
	}
	if s+r < need {
		return "failed"
	}
	return "pending"
}

// Dict renders the state in the same shape as Python ForkState.to_dict (for the shared golden fixture).
func (st *ForkState) Dict() map[string]any {
	branches := []any{}
	for _, id := range st.Order {
		b := st.Branches[id]
		var appr any
		if b.Approval != nil {
			appr = *b.Approval
		}
		branches = append(branches, map[string]any{
			"branch_id": b.BranchID, "node_id": b.NodeID, "capability": b.Capability, "status": b.Status,
			"attempt": b.Attempt, "idempotency_key": b.IdempotencyKey, "reason": b.Reason, "ran": b.Ran,
			"result": b.Result, "receipt": b.Receipt, "approval": appr, "parked": b.Parked})
	}
	closed := any(nil)
	if st.Closed != "" {
		closed = st.Closed
	}
	so := []any{}
	for _, s := range st.SuccessOrder {
		so = append(so, s)
	}
	late := []any{}
	for _, l := range st.LateResults {
		late = append(late, l)
	}
	return map[string]any{"fork_id": st.ForkID, "opened": st.Opened, "spec_digest": st.SpecDigest,
		"join": st.Join, "closed": closed, "close_payload": st.ClosePayload, "branches": branches,
		"success_order": so, "late_results": late}
}
