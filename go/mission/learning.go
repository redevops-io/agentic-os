package mission

// Learning — FOUR loops, not one, plus Reflection before Learning. Capability / Planning / Business
// / Verification-policy learning route off one MissionOutcomeEvent; Reflection emits a structured
// Lesson from the trace. Go port of agentic_os/mission/learning.py.

// LearningRouter routes each mission outcome to the four in-memory loops.
type LearningRouter struct {
	registry      Registry
	capStats      map[string][]int
	planStats     map[string][]int
	business      map[string]float64
	verifyStats   map[string][]int
	RetrievalHook func(MissionOutcomeEvent)
}

func NewLearningRouter(registry Registry) *LearningRouter {
	return &LearningRouter{
		registry: registry, capStats: map[string][]int{}, planStats: map[string][]int{},
		business: map[string]float64{}, verifyStats: map[string][]int{},
	}
}

func (r *LearningRouter) Record(ev MissionOutcomeEvent) {
	s := b2i(ev.Success)
	r.business[ev.Kind] += ev.BusinessValue
	if ev.PlanSignature != "" {
		r.planStats[ev.PlanSignature] = append(r.planStats[ev.PlanSignature], s)
	}
	if r.RetrievalHook != nil {
		r.RetrievalHook(ev)
	}
}

// Attach folds the mission event stream off the serving path (a learner replica).
func (r *LearningRouter) Attach(store *EventStore) { store.Subscribe(r.onEvent) }

func (r *LearningRouter) onEvent(ev *Event) {
	switch ev.Type {
	case "NodeSucceeded":
		cap, _ := ev.Payload["capability"].(string)
		r.RecordCapability(cap, true)
	case "NodeFailed":
		cap, _ := ev.Payload["capability"].(string)
		r.RecordCapability(cap, false)
	case "MissionOutcome":
		kind, _ := ev.Payload["kind"].(string)
		if kind == "" {
			kind = "mission"
		}
		r.business[kind] += toFloat(ev.Payload["business_value"], 0.0)
		if sig, _ := ev.Payload["plan_signature"].(string); sig != "" {
			ok, _ := ev.Payload["success"].(bool)
			r.planStats[sig] = append(r.planStats[sig], b2i(ok))
		}
	}
}

func avgInts(v []int) float64 {
	s := 0
	for _, x := range v {
		s += x
	}
	return roundN(float64(s)/float64(len(v)), 3)
}

// Policy reports what's been learned, per loop — for EXPLAIN / the cockpit.
func (r *LearningRouter) Policy() map[string]any {
	rate := func(stats map[string][]int) map[string]any {
		out := map[string]any{}
		for k, v := range stats {
			if len(v) > 0 {
				out[k] = avgInts(v)
			}
		}
		return out
	}
	biz := map[string]any{}
	for k, v := range r.business {
		biz[k] = v
	}
	return map[string]any{
		"capability":   rate(r.capStats),
		"planning":     rate(r.planStats),
		"business":     biz,
		"verification": rate(r.verifyStats),
	}
}

func (r *LearningRouter) RecordCapability(capability string, success bool) {
	r.capStats[capability] = append(r.capStats[capability], b2i(success))
	if r.registry != nil {
		if cap := r.registry.Get(capability); cap != nil {
			cap.Confidence = avgInts(r.capStats[capability]) // learned confidence feeds the planner
		}
	}
}

// RecordVerification tracks how often a capability's result passes acceptance.
func (r *LearningRouter) RecordVerification(capability string, accepted bool) {
	r.verifyStats[capability] = append(r.verifyStats[capability], b2i(accepted))
}

func (r *LearningRouter) CapabilityConfidence(capability string) (float64, bool) {
	if h := r.capStats[capability]; len(h) > 0 {
		return avgInts(h), true
	}
	return 0, false
}

func (r *LearningRouter) PlanSuccess(signature string) (float64, bool) {
	if h := r.planStats[signature]; len(h) > 0 {
		return avgInts(h), true
	}
	return 0, false
}

func (r *LearningRouter) BusinessValue(kind string) float64 { return r.business[kind] }

// reflectMission scans the trace for the failure signature and emits a structured Lesson; with a
// registry + plan it proposes a concrete alternative capability for the failed outcome.
func reflectMission(missionID string, succeeded bool, timeline []map[string]any, registry Registry, plan *ExecutionPlan) Lesson {
	if succeeded {
		return Lesson{MissionID: missionID, Success: true,
			Evidence: []string{itoa(len(timeline)) + " events, no failures"}}
	}
	var failed, compensated []map[string]any
	hadApproval := false
	for _, e := range timeline {
		switch e["type"] {
		case "NodeFailed":
			failed = append(failed, e)
		case "NodeCompensated":
			compensated = append(compensated, e)
		case "NodeParked":
			hadApproval = true
		}
	}
	cap := ""
	if len(failed) > 0 {
		p, _ := failed[0]["payload"].(map[string]any)
		cap, _ = p["capability"].(string)
	}
	better := ""
	if len(failed) > 0 && registry != nil && plan != nil {
		p, _ := failed[0]["payload"].(map[string]any)
		nid, _ := p["node_id"].(string)
		if node := plan.Graph.ByID(nid); node != nil && node.Produces != "" {
			for _, c := range registry.Providing(node.Produces) {
				if c.Name != cap {
					better = c.Name
					break
				}
			}
		}
	}
	assumption := "plan would complete"
	if cap != "" {
		assumption = "capability '" + cap + "' would succeed"
	}
	missing := ""
	if !hadApproval {
		missing = "no human/verification gate before the failing side effect"
	}
	return Lesson{
		MissionID: missionID, Success: false,
		FailedAssumption: assumption, MissingVerification: missing, BetterCapability: better,
		Evidence: []string{itoa(len(failed)) + " failed node(s), " + itoa(len(compensated)) + " compensated"},
	}
}
