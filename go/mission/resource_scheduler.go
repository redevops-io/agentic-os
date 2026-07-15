package mission

// P11 — Cross-Mission Resource Scheduling: fair-share + admission control across missions competing
// for scarce resources (operators, models, GPUs, quotas, budget, human-approval capacity). Only
// SCARCE resources go in the pool; anything unpooled is unlimited. Deterministic (a logical round
// counter). Go port of agentic_os/mission/resource_scheduler.py.

import (
	"fmt"
	"math"
	"sort"
)

// ResourceRequest is one mission's demand on the shared pools.
type ResourceRequest struct {
	MissionID string
	Demand    map[string]float64 // resource → amount (only pooled keys are rationed)
	Priority  int
	Deadline  *float64 // earliest-deadline-first; nil = no deadline
	Owner     string   // fair-share tenant
}

type waiting struct {
	req          ResourceRequest
	enqueued     int
	roundsWaited int
}

// InfeasibleRequest: demand exceeds total pool capacity for some resource.
type InfeasibleRequest struct{ Msg string }

func (e *InfeasibleRequest) Error() string { return e.Msg }

type CrossMissionScheduler struct {
	capacity  map[string]float64
	available map[string]float64
	agingRate float64
	q         []*waiting
	seq       int
	admitted  map[string]map[string]float64
	served    map[string]float64
}

func NewCrossMissionScheduler(pools map[string]float64, agingRate float64) *CrossMissionScheduler {
	cap := map[string]float64{}
	avail := map[string]float64{}
	for k, v := range pools {
		cap[k] = v
		avail[k] = v
	}
	return &CrossMissionScheduler{capacity: cap, available: avail, agingRate: agingRate,
		admitted: map[string]map[string]float64{}, served: map[string]float64{}}
}

// Submit enqueues a request, rejecting an infeasible demand (> total capacity) up front.
func (s *CrossMissionScheduler) Submit(req ResourceRequest) error {
	for r, amt := range req.Demand {
		if c, pooled := s.capacity[r]; pooled && amt > c {
			return &InfeasibleRequest{Msg: fmt.Sprintf("%s needs %g %s > capacity %g", req.MissionID, amt, r, c)}
		}
	}
	s.seq++
	s.q = append(s.q, &waiting{req: req, enqueued: s.seq})
	return nil
}

// Admit runs one round: age every waiter, then greedily admit the best-ranked fitting requests
// (fair-share → aged priority → EDF → FIFO).
func (s *CrossMissionScheduler) Admit() []string {
	for _, w := range s.q {
		w.roundsWaited++
	}
	ranked := append([]*waiting{}, s.q...)
	sort.SliceStable(ranked, func(i, j int) bool { return s.rankLess(ranked[i], ranked[j]) })

	var admitted []string
	for _, w := range ranked {
		if s.fits(w.req.Demand) {
			s.reserve(w.req.Demand)
			s.admitted[w.req.MissionID] = w.req.Demand
			s.served[w.req.Owner] += weight(w.req.Demand)
			admitted = append(admitted, w.req.MissionID)
		}
	}
	if len(admitted) > 0 {
		var kept []*waiting
		for _, w := range s.q {
			if _, done := s.admitted[w.req.MissionID]; !done {
				kept = append(kept, w)
			}
		}
		s.q = kept
	}
	return admitted
}

func (s *CrossMissionScheduler) rankLess(a, b *waiting) bool {
	if s.served[a.req.Owner] != s.served[b.req.Owner] { // 1. least-served owner (fair share)
		return s.served[a.req.Owner] < s.served[b.req.Owner]
	}
	ea := float64(a.req.Priority) + s.agingRate*float64(a.roundsWaited)
	eb := float64(b.req.Priority) + s.agingRate*float64(b.roundsWaited)
	if ea != eb { // 2. higher aged priority
		return ea > eb
	}
	da, db := math.Inf(1), math.Inf(1) // 3. earliest deadline
	if a.req.Deadline != nil {
		da = *a.req.Deadline
	}
	if b.req.Deadline != nil {
		db = *b.req.Deadline
	}
	if da != db {
		return da < db
	}
	return a.enqueued < b.enqueued // 4. FIFO
}

func (s *CrossMissionScheduler) Release(missionID string) {
	demand, ok := s.admitted[missionID]
	if !ok {
		return
	}
	delete(s.admitted, missionID)
	for r, amt := range demand {
		if c, pooled := s.capacity[r]; pooled {
			s.available[r] = math.Min(c, s.available[r]+amt)
		}
	}
}

func (s *CrossMissionScheduler) fits(demand map[string]float64) bool {
	for r, amt := range demand {
		av, pooled := s.available[r]
		if pooled && av < amt {
			return false
		}
	}
	return true
}

func (s *CrossMissionScheduler) reserve(demand map[string]float64) {
	for r, amt := range demand {
		if _, pooled := s.capacity[r]; pooled {
			s.available[r] -= amt
		}
	}
}

func weight(demand map[string]float64) float64 {
	sum := 0.0
	for _, v := range demand {
		sum += v
	}
	if sum == 0 {
		return 1.0
	}
	return sum
}

func (s *CrossMissionScheduler) Snapshot() map[string]any {
	waitingIDs := make([]string, 0, len(s.q))
	for _, w := range s.q {
		waitingIDs = append(waitingIDs, w.req.MissionID)
	}
	admittedIDs := make([]string, 0, len(s.admitted))
	for id := range s.admitted {
		admittedIDs = append(admittedIDs, id)
	}
	avail := map[string]float64{}
	for k, v := range s.available {
		avail[k] = v
	}
	served := map[string]float64{}
	for k, v := range s.served {
		served[k] = v
	}
	return map[string]any{"available": avail, "waiting": waitingIDs, "admitted": admittedIDs, "served": served}
}

// EstimateDemand derives a mission's resource footprint from its compiled plan.
func EstimateDemand(plan *ExecutionPlan) map[string]float64 {
	g := plan.Graph
	usd := 0.0
	approvals := 0.0
	for i := range g.Nodes {
		usd += g.Nodes[i].Cost.USD
		if g.Nodes[i].ApprovalRequired {
			approvals++
		}
	}
	demand := map[string]float64{
		"usd": roundN(usd, 4), "human_approval": approvals, "slots": float64(len(g.Nodes)),
	}
	for i := range g.Nodes {
		demand["operator:"+g.Nodes[i].Operator]++
	}
	out := map[string]float64{}
	for k, v := range demand {
		if v != 0 {
			out[k] = v
		}
	}
	return out
}
