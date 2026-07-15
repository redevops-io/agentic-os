package mission

// P7A — the Evidence Plane: an append-only kernel primitive over the mission event store. Evidence,
// claims and verification results are first-class because verification, explanation, approvals,
// replay and learning all read from here. Go port of agentic_os/mission/evidence.py.

type EvidenceLog struct{ store *EventStore }

func NewEvidenceLog(store *EventStore) *EvidenceLog { return &EvidenceLog{store} }

func (l *EvidenceLog) RaiseClaim(missionID string, claim Claim) Claim {
	l.store.Append("ClaimRaised", missionID, jsonableMap(claim))
	return claim
}

func (l *EvidenceLog) RecordEvidence(missionID string, ev EvidenceRecord) EvidenceRecord {
	l.store.Append("EvidenceRecorded", missionID, jsonableMap(ev))
	return ev
}

func (l *EvidenceLog) RecordVerification(missionID, nodeID string, vr VerificationResult) {
	p := jsonableMap(vr)
	p["node_id"] = nodeID
	l.store.Append("VerificationRecorded", missionID, p)
}

// Ledger returns the full evidence ledger for a mission (claims · evidence · verifications), in order.
func (l *EvidenceLog) Ledger(missionID string) []map[string]any {
	kinds := map[string]bool{"ClaimRaised": true, "EvidenceRecorded": true, "VerificationRecorded": true}
	var out []map[string]any
	for _, e := range l.store.ForMission(missionID) {
		if kinds[e.Type] {
			m := map[string]any{"type": e.Type}
			for k, v := range e.Payload {
				m[k] = v
			}
			out = append(out, m)
		}
	}
	return out
}
