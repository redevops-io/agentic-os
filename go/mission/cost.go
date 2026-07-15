package mission

// Mission cost model — scores capabilities and plans like a query optimizer scores plans. Used by
// the compiler (best capability per outcome), the simulator (project cost/success) and the
// scheduler/runtime. Go port of agentic_os/mission/cost.py.

import "sort"

var valueScore = map[string]float64{"low": 1.0, "medium": 3.0, "high": 8.0}

const (
	humanMinuteUSD = 0.5    // notional cost of a minute of human attention
	latencyUSDPerS = 0.0002 // notional cost of wall-clock latency
)

// capabilityScore: higher is better — expected value net of cost and risk.
func capabilityScore(c *CapabilitySpec) float64 {
	value, ok := valueScore[c.EstimatedValue]
	if !ok {
		value = 3.0
	}
	money := c.Cost.USD + float64(c.Cost.LatencyMs)/1000*latencyUSDPerS + c.Cost.HumanMinutes*humanMinuteUSD
	risk := 0.0
	if c.SideEffecting {
		risk += 1.0 * (1.0 - c.Confidence) // risky writes cost more
	}
	return c.Confidence*value - money - risk
}

// rankCandidates returns a new slice sorted best-first (stable, so ties keep input order —
// matches Python sorted(..., reverse=True)).
func rankCandidates(cands []*CapabilitySpec) []*CapabilitySpec {
	out := append([]*CapabilitySpec{}, cands...)
	sort.SliceStable(out, func(i, j int) bool { return capabilityScore(out[i]) > capabilityScore(out[j]) })
	return out
}

// graphCost aggregates expected cost over a compiled graph (for the simulator). latency is a
// sequential upper bound; scheduler parallelism lowers it.
func graphCost(g *ExecutionGraph) map[string]any {
	usd := 0.0
	latency := 0
	approvals := 0
	for i := range g.Nodes {
		n := &g.Nodes[i]
		usd += n.Cost.USD
		latency += n.Cost.LatencyMs
		if n.ApprovalRequired {
			approvals++
		}
		usd += n.Cost.HumanMinutes * humanMinuteUSD
	}
	return map[string]any{"usd": roundN(usd, 4), "latency_ms": latency, "approvals": approvals}
}
