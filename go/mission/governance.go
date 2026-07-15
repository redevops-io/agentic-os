package mission

// P10 — Governance plane: policy versioning + an auditable console read-model. The load-bearing work
// is mid-flight policy edits with versioning semantics (the runtime owns the mutations); this is the
// read side. Go port of agentic_os/mission/governance.py.

var govEvents = map[string]bool{
	"PolicyChanged": true, "MissionSuspended": true, "MissionResumed": true,
	"ApprovalInvalidated": true, "PolicyViolationDetected": true, "RecommendationPromoted": true,
}

type GovernanceLog struct{ store *EventStore }

func NewGovernanceLog(store *EventStore) *GovernanceLog { return &GovernanceLog{store} }

// PolicyVersion is 1 at creation, +1 per PolicyChanged — folded from the log, so replay-stable.
func (g *GovernanceLog) PolicyVersion(missionID string) int {
	v := 1
	for _, e := range g.store.ForMission(missionID) {
		if e.Type == "PolicyChanged" {
			v++
		}
	}
	return v
}

func (g *GovernanceLog) PolicyHistory(missionID string) []map[string]any {
	var out []map[string]any
	for _, e := range g.store.ForMission(missionID) {
		if e.Type == "PolicyChanged" {
			out = append(out, withType(e))
		}
	}
	return out
}

// Interventions returns every auditable governance action against a mission, in order.
func (g *GovernanceLog) Interventions(missionID string) []map[string]any {
	var out []map[string]any
	for _, e := range g.store.ForMission(missionID) {
		if govEvents[e.Type] {
			out = append(out, withType(e))
		}
	}
	return out
}

func withType(e *Event) map[string]any {
	m := map[string]any{"type": e.Type}
	for k, v := range e.Payload {
		m[k] = v
	}
	return m
}
