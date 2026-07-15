package mission

// Simulator — dry-run a plan before committing it (EXPLAIN for missions). Projects expected cost /
// success / latency / approvals from capability metadata WITHOUT executing side effects, and gates:
// a plan whose projected cost or approvals exceed the mission budget never runs. Port of simulator.py.

import "fmt"

// Simulate projects a plan's cost/success/latency/approvals against the mission budget.
func Simulate(plan *ExecutionPlan, mission *Mission, registry Registry, policy *SchedulePolicy) SimResult {
	graph := plan.Graph
	usd := 0.0
	humanMinutes := 0.0
	approvals := 0
	pSuccess := 1.0
	var notes []string

	for i := range graph.Nodes {
		n := &graph.Nodes[i]
		conf := 0.9
		if cap := registry.Get(n.Capability); cap != nil {
			conf = cap.Confidence
		}
		pSuccess *= conf
		usd += n.Cost.USD
		humanMinutes += n.Cost.HumanMinutes
		if n.ApprovalRequired {
			approvals++
			humanMinutes += 2.0 // a review costs ~2 human-minutes
		}
		if cap := registry.Get(n.Capability); cap != nil && cap.SideEffecting && conf < 0.7 {
			notes = append(notes, fmt.Sprintf("low-confidence side effect: %s (p=%g)", n.Capability, conf))
		}
	}

	latency := criticalPathLatency(plan)
	humanUSD := humanMinutes * 0.5
	within := (usd+humanUSD) <= mission.Budget.USD && humanMinutes <= mission.Budget.HumanMinutes
	if !within {
		notes = append(notes, fmt.Sprintf("projected spend $%g / %gm exceeds budget $%g / %gm",
			roundN(usd+humanUSD, 3), roundN(humanMinutes, 1), mission.Budget.USD, mission.Budget.HumanMinutes))
	}
	return SimResult{
		ExpectedUSD:       roundN(usd+humanUSD, 4),
		ExpectedSuccess:   roundN(pSuccess, 3),
		ExpectedLatencyMs: latency,
		ExpectedApprovals: approvals,
		WithinBudget:      within,
		Notes:             notes,
	}
}

// criticalPathLatency: sum along the longest dependency chain, not the whole graph.
func criticalPathLatency(plan *ExecutionPlan) int {
	graph := plan.Graph
	memo := map[string]int{}
	var cp func(nid string) int
	cp = func(nid string) int {
		if v, ok := memo[nid]; ok {
			return v
		}
		node := graph.ByID(nid)
		base := 0
		var deps []string
		if node != nil {
			base = node.Cost.LatencyMs
			deps = node.DependsOn
		}
		best := 0
		for _, d := range deps {
			if v := cp(d); v > best {
				best = v
			}
		}
		memo[nid] = base + best
		return memo[nid]
	}
	max := 0
	for i := range graph.Nodes {
		if v := cp(graph.Nodes[i].ID); v > max {
			max = v
		}
	}
	return max
}
