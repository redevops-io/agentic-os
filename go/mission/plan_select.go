package mission

// P9a — Active plan selection: simulation as a first-class planning component. Candidate plans →
// policy pruning → simulation → cost/risk/success scoring → selection, bounded (max candidates,
// admissible providers, one-swap neighbourhood, a switch margin protecting the default). Go port of
// agentic_os/mission/plan_select.py.

import "math"

type PlanCandidate struct {
	Label      string
	Prefer     map[string]string
	Plan       *ExecutionPlan
	Projection SimResult
	Score      float64
	IsDefault  bool
	Selected   bool
}

// scoreProjection folds cost/risk/success into one comparable number (higher = better). Over-budget
// is inadmissible.
func scoreProjection(proj SimResult) float64 {
	if !proj.WithinBudget {
		return math.Inf(-1)
	}
	return proj.ExpectedSuccess*10.0 - proj.ExpectedUSD - 0.5*float64(proj.ExpectedApprovals)
}

type ActivePlanner struct {
	registry      Registry
	policy        SchedulePolicy
	learning      *LearningRouter
	MaxCandidates int
	SwitchMargin  float64
}

func NewActivePlanner(registry Registry, policy SchedulePolicy, learning *LearningRouter) *ActivePlanner {
	return &ActivePlanner{registry: registry, policy: policy, learning: learning, MaxCandidates: 6, SwitchMargin: 0.05}
}

// Select generates candidates, prunes/simulates/scores, and picks — keeping the default unless a
// candidate clearly wins. Returns a CompileError only if even the default plan can't compile.
func (a *ActivePlanner) Select(mission *Mission, intent ExecutionIntent, revision int, reason string) (*ExecutionPlan, []*PlanCandidate, error) {
	var cands []*PlanCandidate
	for _, pp := range a.candidatePrefers(intent) {
		plan, err := CompileIntent(mission, intent, a.registry, revision, reason, pp.prefer)
		if err != nil {
			continue // policy pruning: an inadmissible binding is dropped
		}
		proj := Simulate(plan, mission, a.registry, &a.policy)
		cands = append(cands, &PlanCandidate{Label: pp.label, Prefer: pp.prefer, Plan: plan,
			Projection: proj, Score: scoreProjection(proj), IsDefault: len(pp.prefer) == 0})
	}
	if len(cands) == 0 {
		plan, err := CompileIntent(mission, intent, a.registry, revision, reason, nil)
		if err != nil {
			return nil, nil, err // surface the real CompileError to the caller (fail closed)
		}
		proj := Simulate(plan, mission, a.registry, &a.policy)
		cands = []*PlanCandidate{{Label: "default", Prefer: map[string]string{}, Plan: plan,
			Projection: proj, Score: scoreProjection(proj), IsDefault: true}}
	}

	def := cands[0]
	for _, c := range cands {
		if c.IsDefault {
			def = c
			break
		}
	}
	best := cands[0]
	for _, c := range cands {
		if c.Score > best.Score {
			best = c
		}
	}
	chosen := def
	if best.Score > def.Score+a.SwitchMargin {
		chosen = best
	}
	chosen.Selected = true
	chosen.Plan.Projection = &chosen.Projection
	return chosen.Plan, cands, nil
}

type preferEntry struct {
	label  string
	prefer map[string]string
}

// candidatePrefers: the default plan first, then a bounded one-swap neighbourhood (each alternative
// swaps a single outcome to a non-default admissible provider).
func (a *ActivePlanner) candidatePrefers(intent ExecutionIntent) []preferEntry {
	prefers := []preferEntry{{"default", map[string]string{}}}
	for _, step := range intent.Steps {
		ranked := rankCandidates(a.registry.Providing(step.Outcome))
		for i := 1; i < len(ranked); i++ { // skip the rank-best default provider
			alt := ranked[i]
			prefers = append(prefers, preferEntry{step.Outcome + "=" + alt.Name, map[string]string{step.Outcome: alt.Name}})
			if len(prefers) >= a.MaxCandidates {
				return prefers
			}
		}
	}
	return prefers
}
