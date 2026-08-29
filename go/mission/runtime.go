package mission

// Mission Runtime — the kernel state machine ("what's running now?"). Ties the layers together over
// the event-sourced world-state blackboard: plan → simulate → run (schedule → execute → world
// state) → human gates park / resume → failures compensate (sagas) → outcome → reflection →
// learning. Run() is RESUMABLE: all state lives in the event log. Go port of runtime.py.

import (
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
)

var businessValue = map[string]float64{"onboarding": 100.0, "invoice_recovery": 250.0}

func terminalNode(st NodeState) bool {
	return st == NodeDone || st == NodeSkipped || st == NodeCompensated
}

type MissionRuntime struct {
	registry                 Registry
	executor                 *Executor
	disambiguationConfidence float64
	store                    *EventStore
	planner                  Planner
	scheduler                Scheduler
	policy                   SchedulePolicy
	maxConcurrency           int // executor pool size (default 1 = serial); opt-in parallel wave execution
	learning                 *LearningRouter
	repo                     *MissionRepository
	evidence                 *EvidenceLog
	verifier                 Verifier
	policyPlane              *PolicyDecisionPlane
	context                  *LocalContextRuntime // the single resolution door (bind · policy · belief · model)
	learners                 *LearningStack
	governance               *GovernanceLog
	plans                    map[string]*ExecutionPlan
	missions                 map[string]*Mission
}

type RuntimeOption func(*MissionRuntime)

func WithStore(s *EventStore) RuntimeOption { return func(rt *MissionRuntime) { rt.store = s } }
func WithPlanner(p Planner) RuntimeOption   { return func(rt *MissionRuntime) { rt.planner = p } }
func WithLearning(l *LearningRouter) RuntimeOption {
	return func(rt *MissionRuntime) { rt.learning = l }
}
func WithVerifier(v Verifier) RuntimeOption { return func(rt *MissionRuntime) { rt.verifier = v } }
func WithLearners(l *LearningStack) RuntimeOption {
	return func(rt *MissionRuntime) { rt.learners = l }
}
func WithScheduler(s Scheduler) RuntimeOption { return func(rt *MissionRuntime) { rt.scheduler = s } }

// WithMaxConcurrency opts into parallel wave execution: independent ready nodes' operator invocations run
// concurrently (bounded by n), while the commit stays serial + deterministic. Default 1 = serial.
func WithMaxConcurrency(n int) RuntimeOption { return func(rt *MissionRuntime) { rt.maxConcurrency = n } }
func WithSchedulePolicy(p SchedulePolicy) RuntimeOption {
	return func(rt *MissionRuntime) { rt.policy = p }
}
func WithPolicyPlane(p *PolicyDecisionPlane) RuntimeOption {
	return func(rt *MissionRuntime) { rt.policyPlane = p }
}
func WithDisambiguationConfidence(c float64) RuntimeOption {
	return func(rt *MissionRuntime) { rt.disambiguationConfidence = c }
}

func grantsOf(m *Mission) map[string]bool {
	s := map[string]bool{}
	for _, g := range m.PolicyRefs {
		s[g] = true
	}
	return s
}

func NewMissionRuntime(registry Registry, executor *Executor, opts ...RuntimeOption) *MissionRuntime {
	rt := &MissionRuntime{
		registry: registry, executor: executor, disambiguationConfidence: 0.75,
		planner: TemplatePlanner{}, scheduler: TopoScheduler{}, policy: NewSchedulePolicy(),
		plans: map[string]*ExecutionPlan{}, missions: map[string]*Mission{},
	}
	for _, o := range opts {
		o(rt)
	}
	if rt.maxConcurrency < 1 { // default serial; env opts in (parity with Python/Kotlin)
		if v, err := strconv.Atoi(os.Getenv("AGENTIC_OS_MISSION_CONCURRENCY")); err == nil && v > 0 {
			rt.maxConcurrency = v
		} else {
			rt.maxConcurrency = 1
		}
	}
	if rt.store == nil {
		rt.store = NewEventStore("")
	}
	rt.repo = NewMissionRepository(rt.store)
	rt.evidence = NewEvidenceLog(rt.store)
	rt.governance = NewGovernanceLog(rt.store)
	if rt.learning == nil {
		rt.learning = NewLearningRouter(registry)
	}
	if rt.verifier == nil {
		rt.verifier = NewCompositeVerifier(registry, grantsOf)
	}
	if rt.policyPlane == nil {
		rt.policyPlane = NewPolicyDecisionPlane(registry, rt.learning, grantsOf, rt.evidence)
	}
	// The Mission Runtime decides no context itself — it states a need and the Context Runtime
	// resolves it (bind · policy · belief · model). Mirrors Python runtime.py self.context.
	rt.context = NewLocalContextRuntime(registry)
	rt.context.PolicyPlane = rt.policyPlane
	if rt.learners == nil {
		rt.learners = NewLearningStack()
	}
	return rt
}

func (rt *MissionRuntime) world(missionID string) *WorldState {
	return NewWorldState(missionID, rt.store, Fuse)
}

// ── create + plan + simulate ──────────────────────────────────────────────────

type CreateOpts struct {
	Constraints []string
	PolicyRefs  []string
	Budget      *Budget
	Template    string
}

func (rt *MissionRuntime) CreateMission(goal string, opts CreateOpts) *Mission {
	m := NewMission(goal)
	m.Constraints = opts.Constraints
	m.PolicyRefs = opts.PolicyRefs
	m.Template = opts.Template
	if opts.Budget != nil {
		m.Budget = *opts.Budget
	}
	rt.missions[m.ID] = m
	rt.store.Append("MissionCreated", m.ID, map[string]any{"goal": goal, "template": m.Template, "constraints": m.Constraints})
	if err := rt.planAndGate(m, 1, "initial"); err != nil {
		if _, ok := err.(*CompileError); ok { // stage-1 scoping left no permitted plan — fail closed
			rt.setState(m, MissionFailed)
			rt.store.Append("MissionBlocked", m.ID, map[string]any{"reason": "no_permitted_plan", "detail": err.Error()})
		}
	}
	return m
}

func (rt *MissionRuntime) scoped(m *Mission) *PolicyScopedRegistry {
	return NewPolicyScopedRegistry(rt.registry, m.PolicyRefs)
}

func (rt *MissionRuntime) planAndGate(m *Mission, revision int, reason string) error {
	scoped := rt.scoped(m)
	intent := rt.planner.Plan(m.ID, m.Goal, map[string]any{"template": m.Template})
	selector := NewActivePlanner(scoped, rt.policy, rt.learning)
	plan, candidates, err := selector.Select(m, intent, revision, reason)
	if err != nil {
		return err
	}
	if plan.Projection == nil {
		p := Simulate(plan, m, scoped, &rt.policy)
		plan.Projection = &p
	}
	rt.plans[m.ID] = plan
	m.ActivePlanID = plan.ID
	rt.store.Append("PlanCreated", m.ID, map[string]any{"plan_id": plan.ID, "revision": revision,
		"reason": reason, "signature": rt.signature(plan), "projection": jsonableMap(plan.Projection)})
	if len(candidates) > 1 {
		cands := make([]map[string]any, 0, len(candidates))
		for _, c := range candidates {
			cands = append(cands, map[string]any{"label": c.Label, "score": roundN(c.Score, 3),
				"expected_usd": c.Projection.ExpectedUSD, "expected_success": c.Projection.ExpectedSuccess,
				"within_budget": c.Projection.WithinBudget, "selected": c.Selected})
		}
		rt.store.Append("PlanSelected", m.ID, map[string]any{"plan_id": plan.ID, "considered": len(candidates), "candidates": cands})
	}
	rt.store.Append("PlanActivated", m.ID, map[string]any{"plan_id": plan.ID})
	if !plan.Projection.WithinBudget {
		rt.setState(m, MissionFailed)
		rt.store.Append("MissionBlocked", m.ID, map[string]any{"reason": "over_budget", "notes": plan.Projection.Notes})
	} else {
		rt.setState(m, MissionPlanning)
	}
	return nil
}

// ── run loop (resumable) ──────────────────────────────────────────────────────

func (rt *MissionRuntime) Run(missionID string) *Mission {
	m := rt.missions[missionID]
	if m.State == MissionFailed {
		return m
	}
	plan := rt.plans[missionID]
	graph := plan.Graph
	world := rt.world(missionID)
	rt.setState(m, MissionRunning)
	// Record the effective scheduler config ONCE per mission (startup telemetry) — if this says serial when
	// you expected parallel, the ceiling is the reason, on the record.
	if !rt.hasEvent(missionID, "SchedulerConfigured") {
		rt.store.Append("SchedulerConfigured", m.ID, rt.schedulerConfig())
	}

	for {
		status := rt.repo.NodeStatus(missionID)
		done := map[string]bool{}
		for nid, st := range status {
			if terminalNode(st) {
				done[nid] = true
			}
		}
		approved := rt.approved(missionID)

		allDone := true
		for i := range graph.Nodes {
			if !done[graph.Nodes[i].ID] {
				allDone = false
				break
			}
		}
		if allDone {
			return rt.succeed(m, plan, world)
		}

		batch := rt.scheduler.Ready(graph, done, map[string]bool{}, rt.policy)
		var filtered []*Node
		for _, n := range batch {
			if !done[n.ID] && status[n.ID] != NodeFailed {
				filtered = append(filtered, n)
			}
		}
		batch = filtered
		rt.emitWaveTelemetry(m, graph, done, batch)
		if len(batch) == 0 {
			if rt.repo.PendingHuman(missionID) != nil {
				rt.setState(m, MissionWaitingHuman)
			} else {
				rt.setState(m, MissionFailed) // blocked, no progress possible
			}
			return m
		}

		type gnode struct {
			node     *Node
			decision *ApprovalDecision
		}
		var runnable, gated []gnode
		for _, n := range batch {
			if approved[n.ID] {
				runnable = append(runnable, gnode{n, nil})
				continue
			}
			d := rt.context.Resolve(ContextIntent{Kind: CheckPolicy, Node: n, World: world, Mission: m, Graph: graph}).Value.(ApprovalDecision)
			if d.Required {
				gated = append(gated, gnode{n, &d})
			} else {
				runnable = append(runnable, gnode{n, &d})
			}
		}

		if rt.effectiveMaxConcurrency() > 1 && len(runnable) > 1 {
			// Concurrent wave: independent nodes' I/O overlaps; commit stays serial + deterministic.
			nodes := make([]*Node, len(runnable))
			for i, gn := range runnable {
				nodes[i] = gn.node
			}
			if !rt.executeWave(m, plan, world, nodes) {
				return m
			}
		} else {
			// Serial path — byte-identical to the historical behaviour.
			for _, gn := range runnable {
				if key, b, ok := rt.beliefIssue(gn.node, world); ok {
					rt.parkDisambiguation(m, gn.node, key, b)
					return m
				}
				if !rt.execute(m, plan, world, gn.node) {
					return m
				}
			}
		}
		if len(gated) > 0 {
			rt.park(m, gated[0].node, gated[0].decision)
			return m
		}
		if len(runnable) == 0 {
			rt.setState(m, MissionFailed)
			return m
		}
	}
}

// effectiveMaxConcurrency is the limit that actually binds. The public Go runtime has a single ceiling —
// the scheduler's per-wave release cap — so this is policy.MaxConcurrency (mirrors Python's
// min(pool, policy), where the executor pool is unbounded here). Surfaced so the limit is observable.
func (rt *MissionRuntime) effectiveMaxConcurrency() int {
	e := rt.maxConcurrency // the executor pool
	if rt.policy.MaxConcurrency < e {
		e = rt.policy.MaxConcurrency // ...throttled by the scheduler's per-wave release cap
	}
	if e < 1 {
		return 1
	}
	return e
}

// schedulerConfig is the effective scheduler configuration, recorded once at startup — the guard against a
// silently-serial runtime (Go port of MissionRuntime.scheduler_config).
func (rt *MissionRuntime) schedulerConfig() map[string]any {
	var keyed []string
	for _, c := range rt.registry.All() {
		if c.ConcurrencyKey != "" || len(c.ResourceKeys) > 0 || c.MaxParallelism > 0 || c.ConcurrencyMode != "" {
			keyed = append(keyed, c.Name)
		}
	}
	sort.Strings(keyed)
	eff := rt.effectiveMaxConcurrency()
	pol := "serial"
	if eff > 1 {
		pol = "safe_parallel"
	}
	boundBy := "executor_pool"
	if rt.policy.MaxConcurrency < rt.maxConcurrency {
		boundBy = "schedule_policy"
	}
	return map[string]any{
		"scheduler":                            "TopoScheduler",
		"requested_concurrency":                rt.maxConcurrency,
		"policy_max_concurrency":               rt.policy.MaxConcurrency,
		"effective_max_concurrency":            eff,
		"bound_by":                             boundBy,
		"scheduler_policy":                     pol,
		"capabilities_with_conflict_semantics": keyed,
	}
}

func (rt *MissionRuntime) hasEvent(missionID, typ string) bool {
	for _, e := range rt.repo.Timeline(missionID) {
		if e["type"] == typ {
			return true
		}
	}
	return false
}

// emitWaveTelemetry makes "why was this wave (not) parallel?" auditable (plan §14/§22). Only runs when
// safe-parallel is enabled (effective concurrency > 1): in serial mode every node is held by the cap of 1,
// not a resource conflict, so the per-wave Explain would be pure overhead. Best-effort.
func (rt *MissionRuntime) emitWaveTelemetry(m *Mission, graph *ExecutionGraph, done map[string]bool, batch []*Node) {
	if rt.effectiveMaxConcurrency() <= 1 {
		return
	}
	ex, ok := rt.scheduler.(interface {
		Explain(*ExecutionGraph, map[string]bool, map[string]bool, SchedulePolicy) []ExplainRow
	})
	if !ok {
		return
	}
	rows := ex.Explain(graph, done, map[string]bool{}, rt.policy)
	if len(rows) == 0 {
		return
	}
	var serialized []string
	reasons := map[string]any{}
	for _, r := range rows {
		if r.Decision == "serialized" {
			serialized = append(serialized, r.Node)
			reasons[r.Node] = r.Reason
		}
	}
	rt.store.Append("WaveScheduled", m.ID, map[string]any{
		"runtime_scheduler":    "TopoScheduler",
		"max_parallelism":      rt.effectiveMaxConcurrency(),
		"eligible_nodes":       len(rows),
		"peak_parallel_nodes":  len(batch),
		"serialized_nodes":     serialized,
		"serialization_reason": reasons,
	})
}

func (rt *MissionRuntime) execute(m *Mission, plan *ExecutionPlan, world *WorldState, node *Node) bool {
	result, err := rt.invoke(m, world, node)
	if err != nil {
		return rt.failNode(m, plan, node, err)
	}
	return rt.commit(m, plan, world, node, result)
}

// invoke — the PARALLELIZABLE phase: resolve inputs (reads world) + dispatch + run the operator (the I/O
// wait). Mutates only the thread-safe store + client, so many invokes run at once under a wave.
func (rt *MissionRuntime) invoke(m *Mission, world *WorldState, node *Node) (map[string]any, error) {
	inputs := rt.resolveInputs(m, world, node)
	rt.store.Append("NodeDispatched", m.ID, map[string]any{"node_id": node.ID, "capability": node.Capability})
	return rt.executor.Run(node, inputs)
}

// failNode — the operator raised: record, learn, compensate, fail. Serial (mutates shared state).
func (rt *MissionRuntime) failNode(m *Mission, plan *ExecutionPlan, node *Node, err error) bool {
	rt.store.Append("NodeFailed", m.ID, map[string]any{"node_id": node.ID, "capability": node.Capability, "error": err.Error()})
	rt.learning.RecordCapability(node.Capability, false)
	rt.observeRouting(node, false)
	rt.saga(m, plan)
	rt.fail(m, plan, node.Capability+": "+err.Error())
	return false
}

// commit — apply a successful result: verify before it becomes authoritative, mutate world, emit events.
// SERIAL and in deterministic node order (the concurrent wave calls this after the parallel invokes join),
// so world / learning / event state is identical to the serial path — only the I/O wait overlaps.
func (rt *MissionRuntime) commit(m *Mission, plan *ExecutionPlan, world *WorldState, node *Node, result map[string]any) bool {
	node.Result = result
	// P7B: verify a consequential result BEFORE it becomes authoritative world state.
	if needsVerification(node) && !rt.verifyOK(m.ID)[node.ID] {
		vr := rt.verifier.Verify(node, result, world, m)
		rt.evidence.RecordVerification(m.ID, node.ID, vr)
		rt.learning.RecordVerification(node.Capability, vr.Decision == VerAccept)
		if vr.Decision != VerAccept {
			rt.store.Append("VerificationFailed", m.ID, map[string]any{"node_id": node.ID,
				"capability": node.Capability, "decision": string(vr.Decision), "checks": vr.Checks})
			if vr.Decision == VerReject {
				rt.learning.RecordCapability(node.Capability, false)
				rt.observeRouting(node, false)
				rt.saga(m, plan, node) // v5: the rejected node's own committed effect is compensated too
				rt.fail(m, plan, "verification rejected "+node.Capability)
			} else {
				rt.parkVerification(m, node, vr)
			}
			return false
		}
	}
	node.Status = NodeDone
	if node.Produces != "" {
		world.Observe(node.Produces, result, node.Operator, 1.0)
	}
	// an operator may also REPORT world facts it learned (how conflicting/low-confidence beliefs enter)
	if obsList, ok := result["_observations"].([]any); ok {
		for _, o := range obsList {
			if obs, ok := o.(map[string]any); ok {
				key, _ := obs["key"].(string)
				src, _ := obs["source"].(string)
				if src == "" {
					src = node.Operator
				}
				world.Observe(key, obs["value"], src, toFloat(obs["confidence"], 1.0))
			}
		}
	}
	rt.store.Append("NodeSucceeded", m.ID, map[string]any{"node_id": node.ID, "capability": node.Capability, "result": result})
	rt.learning.RecordCapability(node.Capability, true)
	rt.observeRouting(node, true)
	return true
}

// executeWave runs a ready wave with real parallelism (parity with the Python + Kotlin executors): the
// operator invocations (the I/O wait) run concurrently, bounded by the effective concurrency, behind a
// JOIN BARRIER; then results commit SERIALLY in deterministic node order. Plan fingerprint, node identity,
// idempotency and replay are unaffected — only wall-clock changes. Returns false if the wave parked or
// failed (state already set), mirroring the serial path's `if !execute { return }`.
func (rt *MissionRuntime) executeWave(m *Mission, plan *ExecutionPlan, world *WorldState, wave []*Node) bool {
	// belief-disambiguation gate (serial; reads world). Wave nodes are independent, so their inputs come
	// from pre-wave world state — a disputed input parks before the wave runs.
	for _, n := range wave {
		if key, b, ok := rt.beliefIssue(n, world); ok {
			rt.parkDisambiguation(m, n, key, b)
			return false
		}
	}
	// resolve inputs + dispatch serially in node order (deterministic log), then invoke concurrently.
	inputs := make([]map[string]any, len(wave))
	for i, n := range wave {
		inputs[i] = rt.resolveInputs(m, world, n)
		rt.store.Append("NodeDispatched", m.ID, map[string]any{"node_id": n.ID, "capability": n.Capability})
	}
	results := make([]map[string]any, len(wave))
	errs := make([]error, len(wave))
	sem := make(chan struct{}, rt.effectiveMaxConcurrency())
	var wg sync.WaitGroup
	for i := range wave {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			results[i], errs[i] = rt.executor.Run(wave[i], inputs[i])
		}(i)
	}
	wg.Wait() // JOIN BARRIER — the network waits overlapped; the commit below does not
	for i, n := range wave {
		if errs[i] != nil {
			return rt.failNode(m, plan, n, errs[i])
		}
		if !rt.commit(m, plan, world, n, results[i]) {
			return false
		}
	}
	return true
}

func (rt *MissionRuntime) observeRouting(node *Node, ok bool) {
	if node.Produces != "" {
		rt.learners.Routing.Observe(node.Produces, node.Capability, ok)
	}
}

func (rt *MissionRuntime) resolveInputs(m *Mission, world *WorldState, node *Node) map[string]any {
	resolved := map[string]any{"_goal": m.Goal, "_mission": m.ID}
	for k, v := range node.Inputs {
		if fw, ok := v.(map[string]any); ok {
			if key, ok := fw["$from_world"].(string); ok {
				resolved[k] = world.Get(key)
				continue
			}
		}
		resolved[k] = v
	}
	return resolved
}

// ── human gates ────────────────────────────────────────────────────────────────

func (rt *MissionRuntime) park(m *Mission, node *Node, decision *ApprovalDecision) {
	node.Status = NodeWaiting
	var rules []string
	risk := map[string]any{}
	packet := map[string]any{}
	if decision != nil {
		rules = decision.Rules
		risk = jsonableMap(decision.Risk)
		packet = decision.Packet
	}
	why := ""
	if len(rules) > 0 {
		why = " [" + strings.Join(rules, ", ") + "]"
	}
	task := NewHumanTask(m.ID, node.ID, rt.assignee(node),
		fmt.Sprintf("Approve '%s' (%s)?%s", node.Capability, node.Operator, why))
	task.Evidence = []string{"capability=" + node.Capability,
		fmt.Sprintf("side_effecting=%v", node.SideEffecting), "produces=" + node.Produces}
	rt.store.Append("NodeParked", m.ID, map[string]any{
		"node_id": node.ID, "capability": node.Capability, "rules": rules, "risk": risk, "approval_packet": packet,
		"human_task": map[string]any{"id": task.ID, "assignee": task.Assignee, "prompt": task.Prompt,
			"evidence": task.Evidence, "options": task.Options}})
	rt.setState(m, MissionWaitingHuman)
}

// beliefIssue: a required $from_world input whose fused belief is conflicting or low-confidence.
func (rt *MissionRuntime) beliefIssue(node *Node, world *WorldState) (string, *Belief, bool) {
	keys := make([]string, 0, len(node.Inputs))
	for k := range node.Inputs {
		keys = append(keys, k)
	}
	sort.Strings(keys) // deterministic order (Go maps are unordered)
	for _, k := range keys {
		if fw, ok := node.Inputs[k].(map[string]any); ok {
			if wk, ok := fw["$from_world"].(string); ok {
				b, _ := rt.context.Resolve(ContextIntent{Kind: ResolveBelief, World: world, Key: wk}).Value.(*Belief)
				if b != nil && (b.Conflict || b.Confidence < rt.disambiguationConfidence) {
					return wk, b, true
				}
			}
		}
	}
	return "", nil, false
}

func (rt *MissionRuntime) parkDisambiguation(m *Mission, node *Node, key string, belief *Belief) {
	node.Status = NodeWaiting
	why := fmt.Sprintf("low confidence (%v)", belief.Confidence)
	if belief.Conflict {
		why = "conflicting sources"
	}
	task := NewHumanTask(m.ID, node.ID, "data-steward",
		fmt.Sprintf("Resolve '%s' before '%s' runs — %s.", key, node.Capability, why))
	task.Evidence = []string{"key=" + key, fmt.Sprintf("current=%v", belief.Value),
		fmt.Sprintf("confidence=%v", belief.Confidence), fmt.Sprintf("sources=%v", belief.Sources),
		fmt.Sprintf("conflict=%v", belief.Conflict)}
	task.Options = []string{"resolve"}
	rt.store.Append("NodeParked", m.ID, map[string]any{"node_id": node.ID, "capability": node.Capability,
		"human_task": map[string]any{"id": task.ID, "kind": "disambiguation", "key": key,
			"assignee": task.Assignee, "prompt": task.Prompt, "evidence": task.Evidence, "options": task.Options}})
	rt.store.Append("BeliefDisputed", m.ID, map[string]any{"key": key, "value": belief.Value,
		"sources": belief.Sources, "confidence": belief.Confidence, "conflict": belief.Conflict})
	rt.setState(m, MissionWaitingHuman)
}

func (rt *MissionRuntime) parkVerification(m *Mission, node *Node, vr VerificationResult) {
	node.Status = NodeWaiting
	var evidence []string
	for _, c := range vr.Checks {
		if passed, _ := c["passed"].(bool); !passed {
			evidence = append(evidence, fmt.Sprintf("%v: %v", c["stage"], c["detail"]))
		}
	}
	if len(evidence) == 0 {
		evidence = []string{"assertion(s) unmet"}
	}
	task := NewHumanTask(m.ID, node.ID, "verifier",
		fmt.Sprintf("Verify '%s' before its result is accepted — %d check(s) failed.", node.Capability, len(evidence)))
	task.Evidence = evidence
	task.Options = []string{"accept", "reject"}
	rt.store.Append("NodeParked", m.ID, map[string]any{"node_id": node.ID, "capability": node.Capability,
		"human_task": map[string]any{"id": task.ID, "kind": "verification", "assignee": task.Assignee,
			"prompt": task.Prompt, "evidence": task.Evidence, "options": task.Options}})
	rt.setState(m, MissionWaitingHuman)
}

func (rt *MissionRuntime) verifyOK(missionID string) map[string]bool {
	out := map[string]bool{}
	for _, e := range rt.store.ForMission(missionID) {
		if e.Type == "VerificationOverridden" {
			if nid, ok := e.Payload["node_id"].(string); ok {
				out[nid] = true
			}
		}
	}
	return out
}

// Approve resolves a human gate: disambiguation (authoritative observation), verification
// (accept→commit / reject→fail), or approval (approve→run / reject→fail).
func (rt *MissionRuntime) Approve(missionID, nodeID, decision string, edit map[string]any) *Mission {
	m := rt.missions[missionID]
	pending := rt.repo.PendingHuman(missionID)
	if pending == nil {
		pending = map[string]any{}
	}
	kind, _ := pending["kind"].(string)
	pnid, _ := pending["node_id"].(string)

	if kind == "disambiguation" && pnid == nodeID {
		key, _ := pending["key"].(string)
		var value any
		if edit != nil {
			value = edit["value"]
		}
		rt.world(missionID).Observe(key, value, "human", 100.0)
		rt.store.Append("BeliefResolved", missionID, map[string]any{"key": key, "value": value, "node_id": nodeID})
		rt.store.Append("NodeResumed", missionID, map[string]any{"node_id": nodeID})
		return rt.Run(missionID)
	}
	if kind == "verification" && pnid == nodeID {
		if decision == "accept" || decision == "approve" {
			rt.store.Append("VerificationOverridden", missionID, map[string]any{"node_id": nodeID})
			rt.store.Append("NodeResumed", missionID, map[string]any{"node_id": nodeID})
			return rt.Run(missionID)
		}
		rt.store.Append("VerificationRejected", missionID, map[string]any{"node_id": nodeID})
		rt.store.Append("NodeSkipped", missionID, map[string]any{"node_id": nodeID})
		// this node ran before it escalated — reject compensates its own effect + the prior committed ones (v5)
		plan := rt.plans[missionID]
		rt.saga(m, plan, rt.nodeByID(plan, nodeID))
		rt.fail(m, plan, "verification rejected "+nodeID)
		return m
	}
	if decision == "approve" {
		rt.store.Append("ApprovalGranted", missionID, map[string]any{"node_id": nodeID, "edit": edit})
		rt.store.Append("NodeResumed", missionID, map[string]any{"node_id": nodeID})
		return rt.Run(missionID)
	}
	rt.store.Append("ApprovalRejected", missionID, map[string]any{"node_id": nodeID})
	rt.store.Append("NodeSkipped", missionID, map[string]any{"node_id": nodeID})
	// an approval gate parks BEFORE the node runs, so it has no own effect — but prior committed side effects unwind
	rt.saga(m, rt.plans[missionID])
	rt.fail(m, rt.plans[missionID], "human rejected "+nodeID)
	return m
}

func (rt *MissionRuntime) assignee(node *Node) string { return "approver" }

// ── sagas / failure / success ──────────────────────────────────────────────────

// saga unwinds committed side effects newest-first. It compensates every prior DONE side-effecting
// node, PLUS any `rejected` node passed in — a node whose own result was rolled back (verification
// REJECT, or a human reject after escalation). Per the v5 compensation rule a rejected side-effecting
// node auto-compensates its OWN effect: the operator already ran and committed, but the result never
// became authoritative world state, so the effect must be undone just like a prior stage's.
func (rt *MissionRuntime) saga(m *Mission, plan *ExecutionPlan, rejected ...*Node) {
	compensated := map[string]bool{}
	for _, node := range rejected {
		if node != nil && rt.compensateNode(m, node) {
			compensated[node.ID] = true
		}
	}
	status := rt.repo.NodeStatus(m.ID)
	for i := len(plan.Graph.Nodes) - 1; i >= 0; i-- {
		node := &plan.Graph.Nodes[i]
		if compensated[node.ID] {
			continue
		}
		if status[node.ID] == NodeDone {
			rt.compensateNode(m, node)
		}
	}
}

// compensateNode undoes one node's committed side effect (its undo capability). Returns true if a
// compensation ran. A non-side-effecting node, or one without an undo, is a no-op.
func (rt *MissionRuntime) compensateNode(m *Mission, node *Node) bool {
	if !node.SideEffecting || node.Undo == "" {
		return false
	}
	out := rt.executor.Compensate(node)
	rt.store.Append("NodeCompensated", m.ID, map[string]any{"node_id": node.ID, "undo": node.Undo, "result": out})
	return true
}

// nodeByID resolves a node pointer in a plan's graph (nil if absent).
func (rt *MissionRuntime) nodeByID(plan *ExecutionPlan, id string) *Node {
	if plan == nil {
		return nil
	}
	for i := range plan.Graph.Nodes {
		if plan.Graph.Nodes[i].ID == id {
			return &plan.Graph.Nodes[i]
		}
	}
	return nil
}

func (rt *MissionRuntime) fail(m *Mission, plan *ExecutionPlan, reason string) {
	rt.setState(m, MissionFailed)
	m.Outcome = map[string]any{"success": false, "reason": reason}
	rt.emitOutcome(m, plan, false)
	rt.reflect(m)
}

func (rt *MissionRuntime) succeed(m *Mission, plan *ExecutionPlan, world *WorldState) *Mission {
	rt.setState(m, MissionSucceeded)
	worldMap := map[string]any{}
	for k, b := range world.Snapshot() {
		if b != nil {
			worldMap[k] = b.Value
		} else {
			worldMap[k] = nil
		}
	}
	m.Outcome = map[string]any{"success": true, "world": worldMap}
	rt.emitOutcome(m, plan, true)
	rt.reflect(m)
	return m
}

func (rt *MissionRuntime) emitOutcome(m *Mission, plan *ExecutionPlan, success bool) {
	kind := m.Template
	if kind == "" {
		kind = "mission"
	}
	bv := 0.0
	if success {
		bv = businessValue[kind]
	}
	sig := rt.signature(plan)
	rt.learning.Record(MissionOutcomeEvent{MissionID: m.ID, Kind: kind, Success: success, BusinessValue: bv, PlanSignature: sig})
	rt.learners.Plans.Observe(kind, sig, success)
	rt.store.Append("MissionOutcome", m.ID, map[string]any{"kind": kind, "success": success, "business_value": bv, "plan_signature": sig})
}

func (rt *MissionRuntime) reflect(m *Mission) {
	lesson := reflectMission(m.ID, m.State == MissionSucceeded, rt.repo.Timeline(m.ID), rt.registry, rt.plans[m.ID])
	rt.store.Append("ReflectionEmitted", m.ID, map[string]any{"success": lesson.Success,
		"failed_assumption": lesson.FailedAssumption, "missing_verification": lesson.MissingVerification,
		"better_capability": lesson.BetterCapability, "evidence": lesson.Evidence})
}

// ── cockpit views ──────────────────────────────────────────────────────────────

func (rt *MissionRuntime) Missions() []map[string]any {
	rows := rt.repo.ListMissions()
	for _, r := range rows {
		r["awaiting_human"] = rt.repo.PendingHuman(r["id"].(string)) != nil
	}
	return rows
}

func (rt *MissionRuntime) Inbox() []map[string]any {
	var items []map[string]any
	for _, mid := range rt.store.MissionIDs() {
		if pend := rt.repo.PendingHuman(mid); pend != nil {
			item := map[string]any{"mission_id": mid, "goal": rt.repo.Goal(mid), "kind": "approval"}
			if k, ok := pend["kind"].(string); ok {
				item["kind"] = k
			}
			for k, v := range pend {
				item[k] = v
			}
			items = append(items, item)
		}
	}
	return items
}

// ── P10: governance console ─────────────────────────────────────────────────────

type PolicyChange struct {
	Actor       string
	Reason      string
	Grant       []string
	Revoke      []string
	Constraints []string // nil = unchanged
	Budget      *Budget
	Retroactive bool
}

func (rt *MissionRuntime) ChangePolicy(missionID string, ch PolicyChange) *Mission {
	m := rt.missions[missionID]
	prev := rt.governance.PolicyVersion(missionID)
	before := append([]string{}, m.PolicyRefs...)
	if len(ch.Grant) > 0 {
		set := map[string]bool{}
		for _, g := range m.PolicyRefs {
			set[g] = true
		}
		for _, g := range ch.Grant {
			set[g] = true
		}
		m.PolicyRefs = sortedKeys(set)
	}
	if len(ch.Revoke) > 0 {
		rev := map[string]bool{}
		for _, r := range ch.Revoke {
			rev[r] = true
		}
		var kept []string
		for _, p := range m.PolicyRefs {
			if !rev[p] {
				kept = append(kept, p)
			}
		}
		m.PolicyRefs = kept
	}
	if ch.Constraints != nil {
		m.Constraints = ch.Constraints
	}
	if ch.Budget != nil {
		m.Budget = *ch.Budget
	}

	graph := rt.plans[missionID].Graph
	status := rt.repo.NodeStatus(missionID)
	var affected, ranUnderPrev []string
	for i := range graph.Nodes {
		if terminalNode(status[graph.Nodes[i].ID]) {
			ranUnderPrev = append(ranUnderPrev, graph.Nodes[i].ID)
		} else {
			affected = append(affected, graph.Nodes[i].ID)
		}
	}
	var budgetUSD any
	if ch.Budget != nil {
		budgetUSD = ch.Budget.USD
	}
	rt.store.Append("PolicyChanged", missionID, map[string]any{
		"previous_version": prev, "new_version": prev + 1, "actor": ch.Actor, "reason": ch.Reason,
		"effective_at": now(), "retroactive": ch.Retroactive,
		"changes":        map[string]any{"grant": ch.Grant, "revoke": ch.Revoke, "constraints": ch.Constraints, "budget_usd": budgetUSD},
		"affected_nodes": affected, "ran_under_previous": ranUnderPrev,
		"before_grants": before, "after_grants": append([]string{}, m.PolicyRefs...)})

	if len(ch.Revoke) > 0 { // a tightening invalidates open approvals on still-pending nodes
		affectedSet := map[string]bool{}
		for _, a := range affected {
			affectedSet[a] = true
		}
		candidates := rt.approved(missionID)
		if pend := rt.repo.PendingHuman(missionID); pend != nil {
			if nid, ok := pend["node_id"].(string); ok {
				candidates[nid] = true
			}
		}
		for nid := range candidates {
			if affectedSet[nid] {
				rt.store.Append("ApprovalInvalidated", missionID, map[string]any{"node_id": nid, "reason": "policy_tightened", "version": prev + 1})
			}
		}
	}

	if ch.Retroactive && len(ch.Revoke) > 0 {
		violated := rt.retroactiveViolations(m, ch.Revoke, ranUnderPrev)
		if len(violated) > 0 {
			rt.store.Append("PolicyViolationDetected", missionID, map[string]any{"nodes": violated, "revoked": ch.Revoke, "version": prev + 1})
			rt.saga(m, rt.plans[missionID])
			rt.fail(m, rt.plans[missionID], fmt.Sprintf("retroactive revoke %v", ch.Revoke))
		}
	}
	return m
}

func (rt *MissionRuntime) retroactiveViolations(m *Mission, revoke []string, ranUnderPrev []string) []string {
	rev := map[string]bool{}
	for _, r := range revoke {
		rev[r] = true
	}
	prevSet := map[string]bool{}
	for _, id := range ranUnderPrev {
		prevSet[id] = true
	}
	var out []string
	for i := range rt.plans[m.ID].Graph.Nodes {
		n := &rt.plans[m.ID].Graph.Nodes[i]
		if prevSet[n.ID] && n.SideEffecting {
			if cap := rt.registry.Get(n.Capability); cap != nil {
				for _, p := range cap.Permissions {
					if rev[p] {
						out = append(out, n.ID)
						break
					}
				}
			}
		}
	}
	return out
}

func (rt *MissionRuntime) Suspend(missionID, actor, reason string) *Mission {
	m := rt.missions[missionID]
	rt.store.Append("MissionSuspended", missionID, map[string]any{"actor": actor, "reason": reason})
	rt.setState(m, MissionPaused)
	return m
}

func (rt *MissionRuntime) Resume(missionID, actor string) *Mission {
	m := rt.missions[missionID]
	if m.State != MissionPaused {
		return m
	}
	rt.store.Append("MissionResumed", missionID, map[string]any{"actor": actor})
	return rt.Run(missionID)
}

// PromoteRecommendation is the audited mutation that lets a learned P9 choice affect production.
func (rt *MissionRuntime) PromoteRecommendation(kind, subject, actor string) (*Recommendation, error) {
	recs := rt.learners.Recommendations()
	for i := range recs {
		if recs[i].Kind == kind && recs[i].Subject == subject {
			rec := &recs[i]
			rt.learners.Promote(rec, actor)
			rt.store.Append("RecommendationPromoted", "governance", map[string]any{
				"kind": kind, "subject": subject, "recommended": rec.Recommended, "lift": rec.Lift, "actor": actor})
			return rec, nil
		}
	}
	return nil, fmt.Errorf("no recommendation for %s/%s", kind, subject)
}

// ── P11: cross-mission resource scheduling ──────────────────────────────────────

type BatchOpts struct {
	Pools      map[string]float64
	Priorities map[string]int
	Deadlines  map[string]float64
	Owners     map[string]string
	AgingRate  float64
}

func (rt *MissionRuntime) RunBatch(missionIDs []string, opts BatchOpts) map[string]any {
	aging := opts.AgingRate
	if aging == 0 {
		aging = 1.0
	}
	sched := NewCrossMissionScheduler(opts.Pools, aging)
	for _, mid := range missionIDs {
		req := ResourceRequest{MissionID: mid, Demand: EstimateDemand(rt.plans[mid]),
			Priority: opts.Priorities[mid], Owner: "default"}
		if o, ok := opts.Owners[mid]; ok {
			req.Owner = o
		}
		if d, ok := opts.Deadlines[mid]; ok {
			dd := d
			req.Deadline = &dd
		}
		_ = sched.Submit(req)
	}
	var order []string
	remaining := map[string]bool{}
	for _, mid := range missionIDs {
		remaining[mid] = true
	}
	for len(remaining) > 0 {
		admitted := sched.Admit()
		if len(admitted) == 0 {
			break // infeasible contention, avoid a spin
		}
		for _, mid := range admitted {
			rt.Run(mid)
			sched.Release(mid)
			order = append(order, mid)
			delete(remaining, mid)
		}
	}
	unscheduled := sortedKeys(remaining)
	rt.store.Append("BatchScheduled", "scheduler", map[string]any{"order": order, "unscheduled": unscheduled, "pools": opts.Pools})
	return map[string]any{"order": order, "unscheduled": unscheduled, "final_pools": sched.Snapshot()}
}

func (rt *MissionRuntime) GovernanceConsole() map[string]any {
	var missions []map[string]any
	for _, r := range rt.Missions() {
		mid := r["id"].(string)
		row := map[string]any{}
		for k, v := range r {
			row[k] = v
		}
		row["policy_version"] = rt.governance.PolicyVersion(mid)
		row["grants"] = []string{}
		row["budget_usd"] = nil
		if m := rt.missions[mid]; m != nil {
			row["grants"] = append([]string{}, m.PolicyRefs...)
			row["budget_usd"] = m.Budget.USD
		}
		row["interventions"] = rt.governance.Interventions(mid)
		missions = append(missions, row)
	}
	recs := rt.learners.Recommendations()
	recsJSON := make([]map[string]any, 0, len(recs))
	for i := range recs {
		recsJSON = append(recsJSON, jsonableMap(recs[i]))
	}
	return map[string]any{"missions": missions, "approval_queue": rt.Inbox(), "recommendations": recsJSON}
}

func (rt *MissionRuntime) Explain(missionID string) map[string]any {
	steps := []map[string]any{}
	stepKinds := map[string]bool{"NodeSucceeded": true, "NodeParked": true, "NodeFailed": true,
		"NodeCompensated": true, "BeliefDisputed": true, "BeliefResolved": true, "VerificationFailed": true}
	for _, e := range rt.repo.Timeline(missionID) {
		if stepKinds[e["type"].(string)] {
			steps = append(steps, e)
		}
	}
	world := map[string]any{}
	for k, b := range rt.world(missionID).Snapshot() {
		if b != nil {
			world[k] = b.Value
		} else {
			world[k] = nil
		}
	}
	state := any(nil)
	if st, ok := rt.repo.State(missionID); ok {
		state = string(st)
	}
	recs := rt.learners.Recommendations()
	recsJSON := make([]map[string]any, 0, len(recs))
	for i := range recs {
		recsJSON = append(recsJSON, jsonableMap(recs[i]))
	}
	return map[string]any{"mission_id": missionID, "goal": rt.repo.Goal(missionID), "state": state,
		"steps": steps, "world_state": world, "pending_human": rt.repo.PendingHuman(missionID),
		"evidence": rt.evidence.Ledger(missionID), "learning": rt.learning.Policy(), "recommendations": recsJSON}
}

// Rehydrate rebuilds a mission from the event log after a restart (fresh runtime, same store).
func (rt *MissionRuntime) Rehydrate(missionID string) *Mission {
	var created *Event
	for _, e := range rt.store.ForMission(missionID) {
		if e.Type == "MissionCreated" {
			created = e
			break
		}
	}
	m := NewMission(rt.repo.Goal(missionID))
	m.ID = missionID
	if created != nil {
		m.Template, _ = created.Payload["template"].(string)
		m.Constraints = toStrSlice(created.Payload["constraints"])
	}
	m.PolicyRefs = []string{"*"}
	if st, ok := rt.repo.State(missionID); ok {
		m.State = st
	} else {
		m.State = MissionPlanning
	}
	rt.missions[missionID] = m
	intent := rt.planner.Plan(missionID, m.Goal, map[string]any{"template": m.Template})
	plan, err := CompileIntent(m, intent, rt.scoped(m), 1, "rehydrate", nil)
	if err == nil {
		rt.plans[missionID] = plan
		m.ActivePlanID = plan.ID
	}
	return m
}

// ── helpers ──────────────────────────────────────────────────────────────────

// approved: approved node ids minus any a mid-flight policy change invalidated (P10).
func (rt *MissionRuntime) approved(missionID string) map[string]bool {
	granted, invalidated := map[string]bool{}, map[string]bool{}
	for _, e := range rt.store.ForMission(missionID) {
		nid, _ := e.Payload["node_id"].(string)
		switch e.Type {
		case "ApprovalGranted":
			granted[nid] = true
		case "ApprovalInvalidated":
			invalidated[nid] = true
		}
	}
	for nid := range invalidated {
		delete(granted, nid)
	}
	return granted
}

func (rt *MissionRuntime) signature(plan *ExecutionPlan) string {
	caps := make([]string, 0, len(plan.Graph.Nodes))
	for i := range plan.Graph.Nodes {
		caps = append(caps, plan.Graph.Nodes[i].Capability)
	}
	return strings.Join(caps, "->")
}

func (rt *MissionRuntime) setState(m *Mission, state MissionState) {
	if m.State == state && state != MissionPlanning {
		return
	}
	m.State = state
	m.UpdatedAt = now()
	rt.store.Append("MissionStateChanged", m.ID, map[string]any{"state": string(state)})
}

func sortedKeys(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for k := range set {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
