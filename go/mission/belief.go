package mission

// State estimation — observations are not facts. The CRM, the inbox, a ticket and a human disagree;
// the runtime fuses possibly-conflicting observations of one key into a confidence-weighted BELIEF,
// flagging conflict when high-confidence sources diverge. Go port of agentic_os/mission/belief.py.

import (
	"math"
	"sort"
)

// Fuse fuses observations [{value, source, confidence}] of ONE key into a single Belief. Group by
// value, sum source-confidence per candidate, pick the strongest. confidence = agreement (winner
// mass / total) × reliability (best single source, capped at 1) so BOTH a genuine disagreement and
// a single uncertain source read as low-confidence. conflict when a competing value carries >= 0.34
// of total mass.
func Fuse(observations []map[string]any) Belief {
	if len(observations) == 0 {
		return Belief{Value: nil, Confidence: 0.0}
	}
	mass := map[string]float64{}
	maxconf := map[string]float64{}
	src := map[string][]string{}
	latest := map[string]float64{}
	valueOf := map[string]any{}
	var order []string // first-seen order, for stable tie-breaking (matches Python dict order)

	for _, o := range observations {
		key := hashKey(o["value"])
		if _, seen := valueOf[key]; !seen {
			order = append(order, key)
		}
		valueOf[key] = o["value"]
		c := toFloat(o["confidence"], 1.0)
		mass[key] += c
		if c > maxconf[key] {
			maxconf[key] = c
		}
		s, _ := o["source"].(string)
		if s == "" {
			s = "?"
		}
		if !strContains(src[key], s) {
			src[key] = append(src[key], s)
		}
		if ts := toFloat(o["ts"], 0.0); ts > latest[key] {
			latest[key] = ts
		}
	}

	total := 0.0
	for _, m := range mass {
		total += m
	}
	if total == 0 {
		total = 1.0
	}

	keys := append([]string{}, order...)
	sort.SliceStable(keys, func(i, j int) bool {
		if mass[keys[i]] != mass[keys[j]] {
			return mass[keys[i]] > mass[keys[j]]
		}
		return latest[keys[i]] > latest[keys[j]]
	})

	winKey := keys[0]
	runnerMass := 0.0
	if len(keys) > 1 {
		runnerMass = mass[keys[1]]
	}
	agreement := mass[winKey] / total
	reliability := math.Min(1.0, maxconf[winKey])
	return Belief{
		Value:      valueOf[winKey],
		Confidence: roundN(agreement*reliability, 3),
		Sources:    src[winKey],
		Conflict:   runnerMass >= 0.34*total,
		UpdatedAt:  now(),
	}
}
