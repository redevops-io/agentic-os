package mission

// Saga runner: explicit-only compensation with its own request/authorization/receipt/verification events.
// Go port of agentic_os/mission/compensation_runner.py. It never assumes a step is reversible, never reports a
// mission as rolled back because a compensating action was *attempted*, and leaves irreversible steps alone.
// Per attempt n (counted from the log): idempotency key `{key}:undo:{n}`, read-back key `{key}:undo:{n}:verify`.
// Attempts are capped (AGENTIC_OS_COMPENSATION_MAX_ATTEMPTS, default 3); after the cap only a human acknowledge
// resolves the node. A per-(mission,node) lock serialises concurrent callers.

import (
	"errors"
	"fmt"
	"math"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// OutcomeKeys are the mission-outcome keys owned by the compensation settle step.
var OutcomeKeys = setOf("compensation_incomplete", "compensation_remaining", "compensated", "compensated_unverified",
	"compensation_acknowledged", "compensation_skipped", "not_reversible", "not_compensated", "rolled_back",
	"failure_policy", "partially_completed", "retained_effects", "incomplete_steps", "completed_steps",
	"recovery_actions", "retained_costs", "default_policy_source", "default_policy_warning", "legacy_policy_notice")

const ApprovalTTLSeconds = 3600

const LegacyNotice = "this mission was created before kernel templates defaulted to PRESERVE_ONLY: its frozen default is " +
	"COMPENSATE_DECLARED, so a failure still auto-reverses declared undos (each still needs its own " +
	"authorization). Set AGENTIC_OS_RERESOLVE_LEGACY_POLICY=1 to re-resolve it to the current default."

var (
	compLocksMu sync.Mutex
	compLocks   = map[string]*sync.Mutex{}
)

func compLock(missionID, nodeID string) *sync.Mutex {
	compLocksMu.Lock()
	defer compLocksMu.Unlock()
	k := missionID + "\x00" + nodeID
	if compLocks[k] == nil {
		compLocks[k] = &sync.Mutex{}
	}
	return compLocks[k]
}

// ── wiring ───────────────────────────────────────────────────────────────────

func (rt *MissionRuntime) wireCompensation() {
	if rt.executor == nil || rt.executor.EvidenceProbe != nil {
		return
	}
	rt.executor.EvidenceProbe = rt.compensationEvidence
	rt.executor.UndoRegistry = rt.registeredUndo
	rt.executor.CompRequiresApproval = rt.compensationRequiresApproval
}

func (rt *MissionRuntime) registeredUndo(capability string) string {
	spec := rt.registry.Get(capability)
	if spec == nil {
		return ""
	}
	undo := declaredUndo(spec)
	if undo != "" && rt.registry.Get(undo) != nil {
		return undo
	}
	return ""
}

func (rt *MissionRuntime) compensationRequiresApproval(capability string) bool {
	spec := rt.registry.Get(capability)
	cs := compSpec(spec)
	if cs == nil {
		return false
	}
	if cs.RequiresApproval {
		return true
	}
	if cs.Disposition == DispCleanup && len(cleanupWithoutHumanProblems(spec, rt.registry.Get)) > 0 {
		return true // CLEANUP without the owner-side temporary marker on the UNDO is human-approved
	}
	if cs.Supported && cs.Capability != "" {
		if t := rt.registry.Get(cs.Capability); t != nil && cs.AuthorizationPolicy == AuthEvidence && t.ApprovalRequired {
			return true
		}
	}
	return false
}

// compensationEvidence: authorization evidence folded from the durable event log (never in-memory state).
func (rt *MissionRuntime) compensationEvidence(missionID, nodeID string) CompEvidence {
	var ev CompEvidence
	granted, capOK := false, ""
	for _, e := range rt.store.ForMission(missionID) {
		if payloadStr(e.Payload, "node_id") != nodeID {
			continue
		}
		switch e.Type {
		case "ApprovalGranted":
			granted, capOK = true, payloadStr(e.Payload, "capability")
		case "ApprovalInvalidated":
			granted = false
		case "NodeSucceeded", "BranchSucceeded":
			if truthy(e.Payload["late"]) {
				continue
			}
			capName := payloadStr(e.Payload, "capability")
			if capName == "" {
				if r, ok := e.Payload["receipt"].(map[string]any); ok {
					capName = payloadStr(r, "capability")
				}
			}
			res := e.Payload["result"]
			ok := ProvesExecution(res)
			ev.Executed, ev.Capability = ok, capName
			ev.Result = nil
			if ok {
				ev.Result = res
			}
			ev.Approved = granted && (capOK == "" || capOK == capName)
			ev.CompensatedResult = nil
		case "NodeCompensated":
			if r, ok := e.Payload["result"].(map[string]any); ok && len(r) > 0 {
				ev.CompensatedResult = r
			} else {
				ev.CompensatedResult = map[string]any{"compensated": true}
			}
		}
	}
	return ev
}

// ── helpers ──────────────────────────────────────────────────────────────────

func failReason(out map[string]any) string {
	if out == nil {
		return "undo returned nothing"
	}
	if truthy(out["undo_error"]) {
		return fmt.Sprintf("undo raised: %v", out["undo_error"])
	}
	if IsStaged(out) {
		if _, why := ResultFailed(out, false, true); why != "" {
			return why
		}
		return "undo staged (pending approval), not executed"
	}
	if failed, why := ResultFailed(out, false, true); failed {
		return why
	}
	return ""
}

func preconditionTimeout() time.Duration {
	s, err := strconv.ParseFloat(os.Getenv("AGENTIC_OS_PRECONDITION_TIMEOUT_S"), 64)
	if err != nil {
		s = 30
	}
	return time.Duration(math.Max(0.05, s) * float64(time.Second))
}

func fmtG(f float64) string { return strconv.FormatFloat(f, 'g', -1, 64) }

// verifyReadback -> (verified: nil|true|false, method, detail). Only a declared read-back that returns an explicit
// `verified: true` PLUS the observed state verifies. TRUST LIMIT: observed/digest are checked for PRESENCE only.
func (rt *MissionRuntime) verifyReadback(m *Mission, node *Node, spec *CapabilitySpec, forward any, undoResult map[string]any, attempt int) (*bool, string, string) {
	t, f := true, false
	cs := compSpec(spec)
	if cs == nil || cs.Verify == "" {
		return nil, "", "compensation declares no `verify` read-back"
	}
	vcap := rt.registry.Get(cs.Verify)
	if vcap == nil || vcap.SideEffecting {
		return &f, "readback", fmt.Sprintf("verify '%s' is not a registered read-only capability", cs.Verify)
	}
	op := vcap.Operator
	if op == "" {
		op = node.Operator
	}
	out, err := rt.executor.client.Invoke(op, cs.Verify, map[string]any{"verify_undo_of": node.Capability, "result": forward,
		"undo_result": undoResult, "_node": node.ID + ":undo:verify", "_mission": m.ID},
		fmt.Sprintf("%s:undo:%d:verify", node.IdempotencyKey, attempt))
	if err != nil {
		return &f, "readback", "read-back raised: " + err.Error()
	}
	if bad := failReason(out); bad != "" {
		return &f, "readback", bad
	}
	if v, ok := out["verified"].(bool); !ok || !v {
		return &f, "readback", "read-back did not return an explicit `verified: true`"
	}
	obs := out["observed"]
	empty := obs == nil
	switch x := obs.(type) {
	case string:
		empty = x == ""
	case []any:
		empty = len(x) == 0
	case map[string]any:
		empty = len(x) == 0
	}
	if empty && !truthy(out["digest"]) {
		return &f, "readback", "read-back did not echo the observed state (`observed` or `digest`)"
	}
	return &t, "readback", ""
}

// checkPreconditions: "" when the declared preconditions read-back says reversing is safe NOW, else why not.
// Fail closed: an unregistered / side-effecting / raising / timed-out / unstaged / non-explicit check is "not safe".
func (rt *MissionRuntime) checkPreconditions(m *Mission, node *Node, spec *CapabilitySpec, forward any, attempt int, phase string) string {
	cs := compSpec(spec)
	if cs == nil || cs.Preconditions == "" {
		return ""
	}
	pcap := rt.registry.Get(cs.Preconditions)
	if pcap == nil || pcap.SideEffecting {
		return fmt.Sprintf("precondition_unevaluable: '%s' is not a registered read-only capability", cs.Preconditions)
	}
	op := pcap.Operator
	if op == "" {
		op = node.Operator
	}
	inputs := map[string]any{"precondition_for_undo_of": node.Capability, "undo": node.Undo, "result": forward,
		"_node": node.ID + ":undo:precondition", "_mission": m.ID}
	key := fmt.Sprintf("%s:undo:%d:precondition:%s", node.IdempotencyKey, attempt, phase)
	type box struct {
		out map[string]any
		err error
	}
	ch := make(chan box, 1)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				ch <- box{err: fmt.Errorf("%v", r)}
			}
		}()
		o, e := rt.executor.client.Invoke(op, cs.Preconditions, inputs, key)
		ch <- box{o, e}
	}()
	tmo := preconditionTimeout()
	select {
	case b := <-ch:
		if b.err != nil {
			return "precondition_unevaluable: check raised: " + b.err.Error()
		}
		if bad := failReason(b.out); bad != "" {
			return "precondition_unevaluable: " + bad
		}
		if v, ok := b.out["holds"].(bool); !ok || !v {
			why := payloadStr(b.out, "reason")
			if why == "" {
				why = "the check did not return an explicit `holds: true`"
			}
			return "precondition_not_met: " + why
		}
		return ""
	case <-time.After(tmo):
		return "precondition_unevaluable: check timed out after " + fmtG(tmo.Seconds()) + "s"
	}
}

// policyAllows: may this completed action be considered for compensation under `policy`?
func policyAllows(policy string, spec *CapabilitySpec) bool {
	if policy == PolicyCompensateDeclared {
		return true
	}
	return policy == PolicyPreserveOnly && spec != nil && isCleanup(spec)
}

func (rt *MissionRuntime) legacyNotice(m *Mission, policy string) bool {
	if !(IsLegacyFrozen(m) && policy == PolicyCompensateDeclared) {
		return false
	}
	for _, e := range rt.store.ForMission(m.ID) {
		if e.Type == "LegacyFailurePolicyNotice" {
			return true
		}
	}
	rt.store.Append("LegacyFailurePolicyNotice", m.ID, map[string]any{"template": m.Template,
		"frozen_default": "COMPENSATE_DECLARED", "notice": LegacyNotice})
	return true
}

// maybeReresolveLegacy: operator opt-in (AGENTIC_OS_RERESOLVE_LEGACY_POLICY=1) for an in-flight pre-PRESERVE_ONLY
// mission. RECORDED as DefaultPolicyReresolved and re-applied by rehydrate. Only a legacy COMPENSATE_DECLARED default is
// touched and only toward the template default (it can never widen).
func (rt *MissionRuntime) maybeReresolveLegacy(m *Mission) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(EnvReresolveLegacy))) {
	case "1", "true", "yes":
	default:
		return false
	}
	if !IsLegacyFrozen(m) {
		return false
	}
	to := TemplateFailurePolicy[m.Template]
	if to == "" {
		to = PolicyHaltAndReport
	}
	rt.store.Append("DefaultPolicyReresolved", m.ID, map[string]any{"template": m.Template, "from": m.DefaultFailurePolicy,
		"to": to, "source": EnvReresolveLegacy})
	m.DefaultFailurePolicy, m.DefaultPolicySource = to, "reresolved"
	return true
}

func isForked(plan *ExecutionPlan) bool { return false } // the Go runtime does not open forks (fork.go is replay-only)

// ── run_saga ─────────────────────────────────────────────────────────────────

func (rt *MissionRuntime) runSaga(m *Mission, plan *ExecutionPlan) {
	rt.maybeReresolveLegacy(m)
	status := rt.repo.NodeStatus(m.ID)
	policy := EffectivePolicy(m, isForked(plan))
	forward := map[string]bool{}
	for i := range plan.Graph.Nodes {
		forward[plan.Graph.Nodes[i].Capability] = true
	}
	rt.legacyNotice(m, policy)
	if policy == PolicyCompensateDeclared || policy == PolicyPreserveOnly {
		for i := len(plan.Graph.Nodes) - 1; i >= 0; i-- {
			node := &plan.Graph.Nodes[i]
			if status[node.ID] != NodeDone || !node.SideEffecting || node.Undo == "" {
				continue
			}
			if !policyAllows(policy, rt.registry.Get(node.Capability)) {
				continue
			}
			l := compLock(m.ID, node.ID)
			l.Lock()
			rt.compensateSagaNode(m, node, policy, forward)
			l.Unlock()
		}
	}
	rt.settle(m, plan)
}

func (rt *MissionRuntime) compensateSagaNode(m *Mission, node *Node, policy string, forward map[string]bool) {
	st := FoldCompensation(rt.store.ForMission(m.ID))[node.ID]
	if st != nil && (st.Status == "compensated" || st.Status == "executed_unverified" || st.Status == "acknowledged") {
		return // compensate once: never a second time
	}
	spec := rt.registry.Get(node.Capability)
	if isPreserved(spec) {
		return // PER-ACTION: this effect survives a failed workflow
	}
	pending := st != nil && st.Status == "approval_required"
	if st != nil && (st.Status == "failed" || st.Status == "skipped") && st.Attempts >= compMaxAttempts() {
		return // exhausted: only a human acknowledge clears it
	}
	if pending && st.Approval == nil {
		return // parked, still waiting for the human
	}
	attempt := 1
	if pending {
		attempt = st.Attempt
	} else if st != nil {
		attempt = st.Attempts + 1
	}
	key := fmt.Sprintf("%s:undo:%d", node.IdempotencyKey, attempt)
	cs := compSpec(spec)
	base := func() map[string]any {
		return map[string]any{"node_id": node.ID, "capability": node.Capability, "undo": node.Undo, "idempotency_key": key, "attempt": attempt}
	}
	ev := rt.compensationEvidence(m.ID, node.ID)
	requires := false
	if cs != nil {
		requires = rt.compensationRequiresApproval(node.Capability)
	}
	if !pending {
		p := base()
		p["policy"], p["reversibility"] = policy, ReversibleExplicit
		rt.store.Append(EvCompRequested, m.ID, p)
		if rt.registry.Get(node.Undo) == nil {
			p := base()
			p["warning"] = fmt.Sprintf("declared undo '%s' is not a registered capability; it will not receive the compensation marker (sunset: see docs/COMPENSATION.md)", node.Undo)
			rt.store.Append(EvCompUndeclaredTarget, m.ID, p)
		}
		if node.Undo != node.Capability && !forward[node.Undo] {
			if why := rt.checkPreconditions(m, node, spec, ev.Result, attempt, "plan"); why != "" {
				p := base()
				p["reason"] = why
				rt.store.Append(EvCompSkipped, m.ID, p)
				return // NOT executed: the effect is retained and reported
			}
		}
		if requires && ev.Executed && node.Undo != node.Capability && !forward[node.Undo] {
			inputs := undoInputs(node, m.ID, ev.Result)
			p := base()
			p["inputs_sha256"], p["undo_inputs"], p["cleanup"] = InputsHash(inputs), inputs["result"], isCleanup(spec)
			if isCleanup(spec) {
				p["message"] = fmt.Sprintf("approve CLEANUP of temporary resource %s: run '%s' with exactly these inputs (the rest of the workflow's effects are preserved)", node.ID, node.Undo)
			} else {
				p["message"] = fmt.Sprintf("approve compensation of %s: run '%s' with exactly these inputs", node.ID, node.Undo)
			}
			rt.store.Append(EvCompApprovalRequired, m.ID, p)
			return // PARK: needs its own human approval
		}
	}
	var approval map[string]any
	if pending {
		approval = st.Approval
	}
	if pending && !forward[node.Undo] {
		// the world may have changed while the approval was pending: re-check right before executing
		if why := rt.checkPreconditions(m, node, spec, ev.Result, attempt, "exec"); why != "" {
			p := base()
			p["reason"] = why
			rt.store.Append(EvCompSkipped, m.ID, p)
			return
		}
	}
	registered := rt.registeredUndo(node.Capability)
	var out map[string]any
	reason := ""
	if forward[node.Undo] {
		out = map[string]any{"compensation_skipped": true, "undo_error": "compensation_target_is_forward_action"}
		reason = fmt.Sprintf("compensation_target_is_forward_action: '%s' is also a forward action of this plan", node.Undo)
	} else {
		if ev.Executed && registered == node.Undo && (!requires || approval != nil) {
			basis := "evidence_bound_marker"
			if requires {
				basis = "own_human_approval"
			}
			p := base()
			p["basis"], p["forward_executed"], p["forward_approved"] = basis, true, ev.Approved
			p["requires_approval"], p["registered_undo"] = requires, registered
			if approval != nil {
				p["approved_by"] = approval["approver"]
			} else {
				p["approved_by"] = nil
			}
			rt.store.Append(EvCompAuthorized, m.ID, p)
		}
		out = rt.executor.CompensateFor(node, m.ID, attempt, approval)
		reason = failReason(out)
	}
	if reason == "" {
		p := base()
		p["receipt"], p["result"] = receiptFor(node.ID, node.Undo, key, out), out
		rt.store.Append(EvCompExecuted, m.ID, p)
		verified, method, detail := rt.verifyReadback(m, node, spec, ev.Result, out, attempt)
		if verified != nil && *verified {
			p := base()
			p["method"] = method
			rt.store.Append(EvCompVerified, m.ID, p)
			rt.store.Append("NodeCompensated", m.ID, map[string]any{"node_id": node.ID, "undo": node.Undo, "result": out, "verification": method})
			return
		}
		if verified == nil {
			return // executed, UNVERIFIED: not reported as compensated
		}
		reason = "verification_failed: " + detail
	}
	p := base()
	p["reason"], p["result"] = reason, out
	rt.store.Append(EvCompFailed, m.ID, p)
	// legacy event kept: explain()/needs-you/old consumers read it
	rt.store.Append("NodeCompensationFailed", m.ID, map[string]any{"node_id": node.ID, "undo": node.Undo, "reason": reason, "result": out})
}

// ── outcome: ONE function used live (settle) and on rehydrate, so replay equals the live outcome ──────

func policySource(m *Mission) string {
	if m.FailurePolicy != "" {
		return "mission"
	}
	if m.DefaultPolicySource != "" {
		return m.DefaultPolicySource
	}
	return "legacy_frozen"
}

func strList(xs []string) []any {
	out := make([]any, len(xs))
	for i, x := range xs {
		out[i] = x
	}
	return out
}

func (rt *MissionRuntime) computeOutcome(m *Mission, plan *ExecutionPlan) map[string]any {
	status := rt.repo.NodeStatus(m.ID)
	events := rt.store.ForMission(m.ID)
	folded := FoldCompensation(events)
	s := SummarizeCompensation(folded)
	policy := EffectivePolicy(m, isForked(plan))
	notReversible := []any{}
	notCompensated := []string{}
	retained := []map[string]any{}
	for i := len(plan.Graph.Nodes) - 1; i >= 0; i-- {
		node := &plan.Graph.Nodes[i]
		if status[node.ID] != NodeDone || !node.SideEffecting {
			continue
		}
		spec := rt.registry.Get(node.Capability)
		preserved := isPreserved(spec)
		st := folded[node.ID]
		if node.Undo == "" {
			cls := ReversibilityUnk
			if spec != nil {
				cls = reversibilityOf(spec)
			}
			if cls == NotApplicable {
				cls = ReversibilityUnk
			}
			if node.Irreversible {
				cls = Irreversible
			}
			entry := map[string]any{"node_id": node.ID, "capability": node.Capability, "reversibility": cls}
			if preserved {
				entry["disposition"] = DispPreserve
			}
			notReversible = append(notReversible, entry)
			reason := "undeclared_reversibility"
			if preserved {
				reason = "preserved_by_declaration"
			} else if cls == Irreversible {
				reason = "irreversible"
			}
			retained = append(retained, map[string]any{"node_id": node.ID, "capability": node.Capability, "reason": reason, "detail": irreversibleNoteOf(spec)})
			continue
		}
		// REPLAY: what actually happened is in the log. A node with a compensation record is reported from that
		// record whatever the CURRENT policy resolves to.
		if preserved || (st == nil && !policyAllows(policy, spec)) {
			notCompensated = append(notCompensated, node.ID)
			reason, detail := "left_by_policy", irreversibleNoteOf(spec)
			if preserved {
				reason = "preserved_by_declaration"
			}
			if detail == "" {
				detail = policy
			}
			retained = append(retained, map[string]any{"node_id": node.ID, "capability": node.Capability, "reason": reason, "detail": detail})
			continue
		}
		if st == nil {
			notCompensated = append(notCompensated, node.ID)
			retained = append(retained, map[string]any{"node_id": node.ID, "capability": node.Capability, "reason": "not_yet_compensated",
				"detail": "eligible for compensation but no compensation was recorded"})
			continue
		}
		reason := map[string]string{"skipped": "precondition_not_met", "failed": "compensation_failed",
			"approval_required": "awaiting_compensation_approval", "executed_unverified": "compensation_unverified",
			"acknowledged": "acknowledged"}[st.Status]
		if reason != "" {
			detail := st.Reason
			if detail == "" {
				detail = st.AckReason
			}
			retained = append(retained, map[string]any{"node_id": node.ID, "capability": node.Capability, "reason": reason, "detail": detail})
		}
	}
	remaining := make([]string, 0, len(s.CompensationFailed))
	for n := range s.CompensationFailed {
		remaining = append(remaining, n)
	}
	sort.Strings(remaining)
	out := map[string]any{"failure_policy": policy, "default_policy_source": policySource(m),
		"compensated": strList(s.Compensated), "compensated_unverified": strList(s.CompensatedUnverified),
		"compensation_acknowledged": strList(s.CompensationAck), "not_reversible": notReversible,
		"not_compensated": strList(notCompensated)}
	if m.DefaultPolicyWarning != "" && m.FailurePolicy == "" {
		out["default_policy_warning"] = m.DefaultPolicyWarning
	}
	if IsLegacyFrozen(m) && policy == PolicyCompensateDeclared {
		out["legacy_policy_notice"] = LegacyNotice
	}
	if len(s.CompensationSkipped) > 0 {
		sk := map[string]any{}
		for k, v := range s.CompensationSkipped {
			sk[k] = v
		}
		out["compensation_skipped"] = sk
	}
	if len(remaining) > 0 {
		out["compensation_incomplete"], out["compensation_remaining"] = true, strList(remaining)
	}
	incomplete := []map[string]any{}
	for i := range plan.Graph.Nodes {
		node := &plan.Graph.Nodes[i]
		stt, has := status[node.ID]
		if stt == NodeDone || stt == NodeCompensated {
			continue
		}
		var errStr any
		for j := len(events) - 1; j >= 0; j-- {
			e := events[j]
			if e.Type == "NodeFailed" && payloadStr(e.Payload, "node_id") == node.ID {
				v := e.Payload["error"]
				if !truthy(v) {
					v = e.Payload["reason"]
				}
				if truthy(v) {
					errStr = fmt.Sprintf("%v", v)
				}
				break
			}
		}
		stv := "not_run"
		if has && stt != "" {
			stv = string(stt)
		}
		item := map[string]any{"node_id": node.ID, "capability": node.Capability, "status": stv}
		if errStr != nil {
			item["error"] = errStr
		}
		incomplete = append(incomplete, item)
	}
	if len(retained) > 0 {
		out["retained_effects"] = retainedAny(retained)
		out["partially_completed"] = len(incomplete) > 0
		if len(incomplete) > 0 {
			out["incomplete_steps"] = mapsAny(incomplete)
			completed := []any{}
			for i := range plan.Graph.Nodes {
				n := &plan.Graph.Nodes[i]
				if status[n.ID] == NodeDone {
					completed = append(completed, map[string]any{"node_id": n.ID, "capability": n.Capability, "side_effecting": n.SideEffecting})
				}
			}
			out["completed_steps"] = completed
			out["recovery_actions"] = recoveryActions(incomplete, retained)
		}
	}
	if costs := rt.retainedCosts(m, plan, retained, status); len(costs) > 0 {
		out["retained_costs"] = costs
	}
	out["rolled_back"] = len(s.Compensated) > 0 && s.ReadbackOnly &&
		!(len(remaining) > 0 || len(s.CompensatedUnverified) > 0 || len(s.CompensationAck) > 0 ||
			len(notReversible) > 0 || len(notCompensated) > 0 || len(retained) > 0)
	return out
}

func irreversibleNoteOf(spec *CapabilitySpec) string {
	if spec == nil {
		return ""
	}
	return irreversibleNote(spec)
}

func retainedAny(rs []map[string]any) []any { return mapsAny(rs) }
func mapsAny(rs []map[string]any) []any {
	out := make([]any, len(rs))
	for i, r := range rs {
		out[i] = r
	}
	return out
}

var costIDKeys = []string{"cluster", "service", "target", "resource_id", "resource", "instance_id", "stack", "name", "endpoint"}

func numNonNeg(v any) (float64, bool) {
	if _, isB := v.(bool); isB {
		return 0, false
	}
	switch x := v.(type) {
	case float64:
		return x, x >= 0
	case int:
		return float64(x), x >= 0
	case int64:
		return float64(x), x >= 0
	}
	if n, ok := v.(interface{ Float64() (float64, error) }); ok {
		f, err := n.Float64()
		return f, err == nil && f >= 0
	}
	return 0, false
}

func firstNum(m map[string]any, keys ...string) (float64, bool) {
	for _, k := range keys {
		if f, ok := numNonNeg(m[k]); ok {
			return f, true
		}
	}
	return 0, false
}

func round4(f float64) float64 { return math.Round(f*1e4) / 1e4 }
func round2(f float64) float64 { return math.Round(f*1e2) / 1e2 }

// retainedCosts: resources still provisioned (and costing money) after a failed mission. Cost comes ONLY from the
// forward result; with none it is UNKNOWN (nil + cost_known false). A number is never invented.
func (rt *MissionRuntime) retainedCosts(m *Mission, plan *ExecutionPlan, retained []map[string]any, status map[string]NodeState) map[string]any {
	type cand struct {
		node   *Node
		failed bool
	}
	var cands []cand
	for _, r := range retained {
		if n := plan.Graph.ByID(payloadStr(r, "node_id")); n != nil {
			cands = append(cands, cand{n, false})
		}
	}
	for i := range plan.Graph.Nodes {
		n := &plan.Graph.Nodes[i]
		if status[n.ID] == NodeFailed || status[n.ID] == NodeRunning {
			cands = append(cands, cand{n, true})
		}
	}
	items := []map[string]any{}
	for _, c := range cands {
		spec := rt.registry.Get(c.node.Capability)
		if spec == nil || !incursOngoingCost(spec) {
			continue
		}
		var res map[string]any
		if !c.failed {
			if r, ok := rt.compensationEvidence(m.ID, c.node.ID).Result.(map[string]any); ok {
				res = r
			}
		}
		if res == nil {
			res = map[string]any{}
		}
		src := map[string]any{}
		for k, v := range c.node.Inputs {
			src[k] = v
		}
		if sp, ok := src["spec"].(map[string]any); ok {
			merged := map[string]any{}
			for k, v := range sp {
				merged[k] = v
			}
			for k, v := range src {
				merged[k] = v
			}
			src = merged
		}
		ids := map[string]any{}
		if c.failed {
			for _, k := range costIDKeys {
				switch v := src[k].(type) {
				case string:
					if v != "" {
						ids[k] = v
					}
				case int, int64, float64:
					ids[k] = v
				}
			}
		} else {
			for _, k := range costIDKeys {
				if v := res[k]; v != nil && !isEmptyVal(v) {
					ids[k] = v
				}
			}
		}
		var hourly, monthly *float64
		why := "unknown"
		switch {
		case c.failed:
			why = "unknown: the step failed, resources may exist"
		case truthy(res["failed_over"]):
			why = "unknown: launch failed over, the approved estimate describes another placement"
		default:
			h, hok := firstNum(res, "hourly_usd", "estimated_hourly_usd")
			mo, mok := firstNum(res, "est_monthly_usd", "estimated_monthly_usd")
			if !hok && mok {
				h, hok = round4(mo/730), true
			}
			if !mok && hok {
				mo, mok = round2(h*730), true
			}
			if hok {
				hourly, why = &h, "forward result (approved estimate)"
			}
			if mok {
				monthly = &mo
			}
		}
		it := map[string]any{"node_id": c.node.ID, "capability": c.node.Capability, "resource": ids,
			"estimated_hourly_usd": ptrOrNil(hourly), "estimated_monthly_usd": ptrOrNil(monthly),
			"cost_known": hourly != nil, "cost_source": why, "possibly_provisioned": c.failed,
			"action_to_stop_cost": c.node.Undo, "note": "teardown is a separate, explicitly authorized action"}
		it["line"] = costLine(it, ids, hourly, monthly)
		items = append(items, it)
	}
	if len(items) == 0 {
		return nil
	}
	sumH, sumM, anyKnown := 0.0, 0.0, false
	unknown := []any{}
	complete := true
	lines := []string{}
	for _, it := range items {
		if it["cost_known"].(bool) {
			anyKnown = true
			sumH += it["estimated_hourly_usd"].(float64)
			if v, ok := it["estimated_monthly_usd"].(float64); ok {
				sumM += v
			}
		} else {
			unknown = append(unknown, it["node_id"])
			complete = false
		}
		lines = append(lines, it["line"].(string))
	}
	out := map[string]any{"resources": mapsAny(items), "estimated_hourly_usd": nil, "estimated_monthly_usd": nil,
		"unknown_cost_resources": unknown, "complete": complete, "summary": strings.Join(lines, "; ")}
	if anyKnown {
		out["estimated_hourly_usd"], out["estimated_monthly_usd"] = round4(sumH), round2(sumM)
	}
	return out
}

func ptrOrNil(f *float64) any {
	if f == nil {
		return nil
	}
	return *f
}

func isEmptyVal(v any) bool {
	switch x := v.(type) {
	case string:
		return x == ""
	case []any:
		return len(x) == 0
	case map[string]any:
		return len(x) == 0
	}
	return false
}

func costLine(it map[string]any, ids map[string]any, hourly, monthly *float64) string {
	keys := make([]string, 0, len(ids))
	for _, k := range costIDKeys { // dict order == insertion order == _ID_KEYS order in Python
		if _, ok := ids[k]; ok {
			keys = append(keys, k)
		}
	}
	parts := make([]string, len(keys))
	for i, k := range keys {
		parts[i] = fmt.Sprintf("%s=%v", k, ids[k])
	}
	who := strings.Join(parts, ", ")
	if who == "" {
		who = "identifiers unknown"
	}
	stop := payloadStr(it, "action_to_stop_cost")
	if stop == "" {
		stop = "a manual action"
	}
	if hourly != nil {
		return fmt.Sprintf("%s [%s]: still provisioned, ~$%s/h (~$%s/month, %s); stop with %s", it["capability"], who,
			pyNum(*hourly), pyNum(ptrVal(monthly)), it["cost_source"], stop)
	}
	state := "still provisioned"
	if it["possibly_provisioned"].(bool) {
		state = "POSSIBLY provisioned (the step failed)"
	}
	return fmt.Sprintf("%s [%s]: %s, COST UNKNOWN (%s); check the provider and stop with %s", it["capability"], who, state, it["cost_source"], stop)
}

func ptrVal(f *float64) float64 {
	if f == nil {
		return 0
	}
	return *f
}

// pyNum renders a float like Python str(float): integral values keep ".0".
func pyNum(f float64) string { return pyFloat(f) }

func recoveryActions(incomplete, retained []map[string]any) []any {
	steps := []any{}
	caps := []any{}
	for _, i := range incomplete {
		steps = append(steps, i["node_id"])
		caps = append(caps, i["capability"])
	}
	acts := []any{map[string]any{"action": "start_new_mission_for_remaining_steps", "steps": steps, "capabilities": caps,
		"detail": "a FAILED mission is terminal and cannot be resumed: start a NEW mission covering only these " +
			"remaining steps (the completed steps listed in completed_steps are already in place)"}}
	var left, other []any
	for _, r := range retained {
		switch r["reason"] {
		case "left_by_policy":
			left = append(left, r["node_id"])
		case "irreversible", "undeclared_reversibility", "preserved_by_declaration":
			other = append(other, r["node_id"])
		}
	}
	if len(left) > 0 {
		acts = append(acts, map[string]any{"action": "decide_explicit_compensation", "steps": left,
			"detail": "retained by policy; undoing them needs an explicit failure_policy opt-in (or a separate forward action) plus its own authorization"})
	}
	if len(other) > 0 {
		acts = append(acts, map[string]any{"action": "manual_remediation_if_unwanted", "steps": other, "detail": "not reversible by the system, or declared PRESERVE"})
	}
	return acts
}

func deepEqualJSON(a, b any) bool { return pyJSON(a) == pyJSON(b) }

// recordRetained appends the retained-effects record to the durable log when it CHANGED, once per distinct state.
func (rt *MissionRuntime) recordRetained(m *Mission, outcome map[string]any) {
	retained, _ := outcome["retained_effects"].([]any)
	costs, _ := outcome["retained_costs"].(map[string]any)
	if len(retained) == 0 && len(costs) == 0 {
		return
	}
	events := rt.store.ForMission(m.ID)
	var last map[string]any
	for i := len(events) - 1; i >= 0; i-- {
		if events[i].Type == EvEffectsRetained {
			last = events[i].Payload
			break
		}
	}
	inc, _ := outcome["incomplete_steps"].([]any)
	if inc == nil {
		inc = []any{}
	}
	if costs == nil {
		costs = map[string]any{}
	}
	if last != nil && deepEqualJSON(last["retained_effects"], retained) && deepEqualJSON(last["incomplete_steps"], inc) &&
		deepEqualJSON(last["retained_costs"], costs) {
		return
	}
	completed, _ := outcome["completed_steps"].([]any)
	if completed == nil {
		completed = []any{}
	}
	ra, _ := outcome["recovery_actions"].([]any)
	if ra == nil {
		ra = []any{}
	}
	pc, _ := outcome["partially_completed"].(bool)
	rt.store.Append(EvEffectsRetained, m.ID, map[string]any{"partially_completed": pc, "retained_effects": retained,
		"incomplete_steps": inc, "failure_policy": outcome["failure_policy"], "completed_steps": completed,
		"recovery_actions": ra, "retained_costs": costs})
}

// settle recomputes the compensation part of m.Outcome from the log.
func (rt *MissionRuntime) settle(m *Mission, plan *ExecutionPlan) {
	out := map[string]any{}
	for k, v := range m.Outcome {
		if !OutcomeKeys[k] {
			out[k] = v
		}
	}
	for k, v := range rt.computeOutcome(m, plan) {
		out[k] = v
	}
	m.Outcome = out
	rt.recordRetained(m, out)
}

// outcomeFromLog: compensation outcome fields rebuilt from the durable log (rehydrate / explain).
func (rt *MissionRuntime) outcomeFromLog(missionID string) map[string]any {
	m, plan := rt.missions[missionID], rt.plans[missionID]
	if m == nil || plan == nil {
		return map[string]any{}
	}
	return rt.computeOutcome(m, plan)
}

// ── operator interventions ───────────────────────────────────────────────────

// InterventionError: wrong state / unknown node / nothing to do.
type InterventionError struct{ Msg string }

func (e *InterventionError) Error() string { return e.Msg }

func (rt *MissionRuntime) intervene(m *Mission, action, actor, reason, nodeID string) error {
	folded := FoldCompensation(rt.store.ForMission(m.ID))
	plan := rt.plans[m.ID]
	nowT := now()
	var targets []string
	sortedNodes := func(pred func(*CompStatus) bool) []string {
		var out []string
		for n, v := range folded {
			if pred(v) && (nodeID == "" || n == nodeID) {
				out = append(out, n)
			}
		}
		sort.Strings(out)
		return out
	}
	switch action {
	case "retry":
		targets = sortedNodes(func(v *CompStatus) bool {
			return (v.Status == "failed" || v.Status == "skipped") && v.Attempts < compMaxAttempts()
		})
		if len(targets) == 0 {
			return &InterventionError{"no failed compensation is retryable (none failed, or max attempts reached: acknowledge instead)"}
		}
		rt.store.Append("CompensationRetryRequested", m.ID, map[string]any{"actor": actor, "reason": reason, "node_ids": strList(targets), "at": nowT})
		rt.runSaga(m, plan)
	case "approve":
		v := folded[nodeID]
		if v == nil || v.Status != "approval_required" {
			return &InterventionError{"that node has no compensation awaiting approval"}
		}
		node := plan.Graph.ByID(nodeID)
		if node == nil {
			return &InterventionError{"that node has no compensation awaiting approval"}
		}
		ev := rt.compensationEvidence(m.ID, nodeID)
		inputs := undoInputs(node, m.ID, ev.Result)
		digest := InputsHash(inputs)
		if digest != v.InputsSHA256 {
			return &InterventionError{"the compensation inputs changed since approval was requested"}
		}
		rt.store.Append(EvCompApproved, m.ID, map[string]any{"node_id": nodeID, "mission_id": m.ID, "undo": node.Undo,
			"attempt": v.Attempt, "inputs_sha256": digest, "approver": actor, "reason": reason, "at": nowT, "exp": nowT + ApprovalTTLSeconds})
		rt.runSaga(m, plan)
	case "acknowledge", "verify_manually":
		want := map[string]bool{"failed": true, "approval_required": true, "executed_unverified": true, "skipped": true}
		if action == "verify_manually" {
			want = map[string]bool{"executed_unverified": true}
		}
		targets = sortedNodes(func(v *CompStatus) bool { return want[v.Status] })
		if len(targets) == 0 {
			return &InterventionError{"nothing to " + strings.ReplaceAll(action, "_", " ")}
		}
		for _, nid := range targets {
			if action == "acknowledge" {
				rt.store.Append(EvCompAcknowledged, m.ID, map[string]any{"node_id": nid, "actor": actor, "reason": reason, "undo": folded[nid].Undo, "at": nowT})
			} else {
				rt.store.Append(EvCompVerified, m.ID, map[string]any{"node_id": nid, "undo": folded[nid].Undo, "method": "manual", "actor": actor, "reason": reason, "at": nowT})
			}
		}
		rt.settle(m, plan)
	default:
		return &InterventionError{fmt.Sprintf("unknown compensation action '%s'", action)}
	}
	return nil
}

// CompensationIntervention is the guarded operator path for a mission that needs a human after compensation
// (retry | approve | acknowledge | verify_manually). Every call records who/when/why in the log.
func (rt *MissionRuntime) CompensationIntervention(missionID, action, actor, reason, nodeID string) (*Mission, error) {
	m := rt.missions[missionID]
	if m == nil {
		return nil, errors.New("unknown mission")
	}
	if m.State != MissionCompensationFailed && m.State != MissionFailed {
		return m, &InterventionError{fmt.Sprintf("mission is %s; compensation interventions apply to a failed mission", m.State)}
	}
	if err := rt.intervene(m, action, actor, reason, nodeID); err != nil {
		return m, err
	}
	if truthy(m.Outcome["compensation_incomplete"]) {
		rt.setState(m, MissionCompensationFailed)
	} else {
		rt.setState(m, MissionFailed)
	}
	return m, nil
}

// AttentionItems: needs-you items for compensation that needs a human.
func (rt *MissionRuntime) AttentionItems(missionID string) []map[string]any {
	folded := FoldCompensation(rt.store.ForMission(missionID))
	goal := rt.repo.Goal(missionID)
	items := []map[string]any{}
	ids := make([]string, 0, len(folded))
	for n := range folded {
		ids = append(ids, n)
	}
	sort.Strings(ids)
	for _, nid := range ids {
		v := folded[nid]
		base := func(extra map[string]any) map[string]any {
			b := map[string]any{"mission_id": missionID, "node_id": nid, "goal": goal}
			for k, e := range extra {
				b[k] = e
			}
			return b
		}
		switch v.Status {
		case "approval_required":
			var p map[string]any
			evs := rt.store.ForMission(missionID)
			for i := len(evs) - 1; i >= 0; i-- {
				if evs[i].Type == EvCompApprovalRequired && payloadStr(evs[i].Payload, "node_id") == nid {
					p = evs[i].Payload
					break
				}
			}
			cleanup := truthy(p["cleanup"])
			title, why := "Approve compensation of "+nid, ""
			if cleanup {
				title, why = "Approve CLEANUP of temporary resource "+nid, "CLEANUP of a temporary resource only (other effects are preserved): "
			}
			items = append(items, base(map[string]any{"kind": "compensation_approval", "title": title,
				"why":  fmt.Sprintf("%srun '%s' (attempt %d) with exactly these inputs", why, v.Undo, v.Attempt),
				"undo": v.Undo, "inputs": p["undo_inputs"], "inputs_sha256": p["inputs_sha256"],
				"decisions": []any{"approve_compensation", "acknowledge"}}))
		case "failed":
			dec := []any{"retry_compensation", "acknowledge"}
			if v.Attempts >= compMaxAttempts() {
				dec = []any{"acknowledge"}
			}
			items = append(items, base(map[string]any{"kind": "compensation_failed", "title": "Compensation failed: " + v.Undo,
				"why": v.Reason, "attempts": v.Attempts, "decisions": dec}))
		case "skipped":
			items = append(items, base(map[string]any{"kind": "compensation_skipped", "title": "Compensation not safe now: " + v.Undo,
				"why": v.Reason, "decisions": []any{"retry_compensation", "acknowledge"}}))
		case "executed_unverified":
			items = append(items, base(map[string]any{"kind": "compensation_unverified", "title": "Verify compensation: " + v.Undo,
				"why": "the compensating action ran but nothing proved the effect is gone", "decisions": []any{"verify_manually", "acknowledge"}}))
		}
	}
	m := rt.missions[missionID]
	if m == nil {
		return items
	}
	out := m.Outcome
	left, _ := out["not_compensated"].([]any)
	var actionable []any
	if re, ok := out["retained_effects"].([]any); ok {
		for _, r := range re {
			rm, _ := r.(map[string]any)
			switch rm["reason"] {
			case "preserved_by_declaration", "precondition_not_met", "left_by_policy":
				actionable = append(actionable, r)
			}
		}
	}
	costs, _ := out["retained_costs"].(map[string]any)
	if costs == nil {
		costs = map[string]any{}
	}
	ack, _ := out["compensation_acknowledged"].([]any)
	if m.State == MissionFailed && (len(left) > 0 || len(actionable) > 0 || len(costs) > 0) && len(ack) == 0 {
		n := len(left)
		if n == 0 {
			n = len(actionable)
		}
		if n == 0 {
			if rs, ok := costs["resources"].([]any); ok {
				n = len(rs)
			}
		}
		cs, _ := costs["summary"].(string)
		if cs == "" {
			cs = "no cost-bearing resource among the retained effects"
		}
		item := map[string]any{"mission_id": missionID, "goal": goal, "kind": "effects_left_in_place",
			"title":           fmt.Sprintf("Mission partially completed; %d effect(s) were left in place", n),
			"why":             fmt.Sprintf("failure policy %v: succeeded effects are kept, not reversed", out["failure_policy"]),
			"not_compensated": left, "not_reversible": out["not_reversible"], "retained_effects": out["retained_effects"],
			"incomplete_steps": out["incomplete_steps"], "completed_steps": out["completed_steps"],
			"recovery_actions": out["recovery_actions"], "retained_costs": costs, "cost_summary": cs,
			"decisions": []any{"manual_remediation", "escalate"}}
		if x := out["legacy_policy_notice"]; x != nil {
			item["legacy_policy_notice"] = x
		}
		if x := out["default_policy_warning"]; x != nil {
			item["default_policy_warning"] = x
		}
		items = append(items, item)
	}
	return items
}
