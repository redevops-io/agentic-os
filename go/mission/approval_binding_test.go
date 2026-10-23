package mission

// Approvals are bound to the inputs the approver was shown (Python: runtime._park/_approved_digest/dispatch recheck) and
// a parked gate can lapse (AGENTIC_OS_APPROVAL_TTL_S).

import (
	"errors"
	"testing"
	"time"
)

// parkedGate runs a gated template to its human gate and returns the runtime, mission id and the gated node.
func parkedGate(t *testing.T) (*MissionRuntime, string, *Node) {
	t.Helper()
	rt := everyMissionRuntime()
	m := rt.CreateMission("gate", CreateOpts{PolicyRefs: []string{"*"}, Template: "deploy_app"})
	rt.Run(m.ID)
	if rt.repoState(t, m.ID) != MissionWaitingHuman {
		t.Fatalf("not parked: %v", rt.repoState(t, m.ID))
	}
	pend := rt.repo.PendingHuman(m.ID)
	node := rt.nodeByID(rt.plans[m.ID], pend["node_id"].(string))
	if node == nil {
		t.Fatal("gated node not found")
	}
	return rt, m.ID, node
}

func lastEvent(rt *MissionRuntime, mid, typ string) *Event {
	var out *Event
	for _, e := range rt.store.ForMission(mid) {
		if e.Type == typ {
			out = e
		}
	}
	return out
}

func countEvents(rt *MissionRuntime, mid, typ string) int {
	n := 0
	for _, e := range rt.store.ForMission(mid) {
		if e.Type == typ {
			n++
		}
	}
	return n
}

func TestParkedPacketCarriesADigestAndTheFactsTheApproverNeeds(t *testing.T) {
	rt, mid, node := parkedGate(t)
	node.Inputs = map[string]any{"amount": 100, "currency": "USD", "to": "jane.doe@acme.com", "secret_token": "s3"}
	// re-park to record the facts for these inputs
	rt.park(rt.missions[mid], node, nil)
	p := lastEvent(rt, mid, "NodeParked").Payload
	if d, _ := p["inputs_sha256"].(string); len(d) != 64 {
		t.Fatalf("no digest: %v", p["inputs_sha256"])
	}
	sum, _ := p["action_summary"].(map[string]any)
	if sum["amount"] != 100 || sum["currency"] != "USD" || sum["to"] != "ja***@acme.com" {
		t.Fatalf("summary %v", sum)
	}
	if _, leaked := sum["secret_token"]; leaked {
		t.Fatal("a non-allowlisted input leaked into the summary")
	}
}

func TestApprovalRecordsTheDigestAndActorAndUnchangedInputsRun(t *testing.T) {
	rt, mid, node := parkedGate(t)
	shown := rt.parkedDigest(mid, node.ID)
	if len(shown) != 64 {
		t.Fatal("park recorded no digest")
	}
	if _, err := rt.ApproveBy(mid, node.ID, "approve", nil, "approver:acme"); err != nil {
		t.Fatal(err)
	}
	g := lastEvent(rt, mid, "ApprovalGranted").Payload
	if g["inputs_sha256"] != shown || g["approver"] != "approver:acme" {
		t.Fatalf("granted payload %v", g)
	}
	if countEvents(rt, mid, "ApprovalInvalidated") != 0 {
		t.Fatal("unchanged inputs must not invalidate")
	}
}

func TestInputsChangedBetweenParkAndApproveDoNotRunAndParkAgain(t *testing.T) {
	rt, mid, node := parkedGate(t)
	node.Inputs = map[string]any{"amount": 100}
	rt.park(rt.missions[mid], node, nil) // the human is shown amount=100
	node.Inputs = map[string]any{"amount": 1000}
	rt.Approve(mid, node.ID, "approve", nil)
	if countEvents(rt, mid, "ApprovalInvalidated") != 1 {
		t.Fatal("changed inputs must invalidate the approval")
	}
	if rt.repoState(t, mid) != MissionWaitingHuman {
		t.Fatalf("state %v, want waiting for a new decision", rt.repoState(t, mid))
	}
	for _, e := range rt.store.ForMission(mid) {
		if e.Type == "NodeDispatched" && e.Payload["node_id"] == node.ID {
			t.Fatal("the gated node ran with inputs the approver never saw")
		}
	}
	sum, _ := lastEvent(rt, mid, "NodeParked").Payload["action_summary"].(map[string]any)
	if sum["amount"] != 1000 {
		t.Fatalf("second review must show the real amount, got %v", sum)
	}
	rt.Approve(mid, node.ID, "approve", nil) // a fresh approval of the new digest runs
	ran := false
	for _, e := range rt.store.ForMission(mid) {
		if e.Type == "NodeDispatched" && e.Payload["node_id"] == node.ID {
			ran = true
		}
	}
	if !ran {
		t.Fatal("a fresh approval of the new inputs should release the node")
	}
}

func TestLegacyApprovalWithoutADigestStillRuns(t *testing.T) {
	rt, mid, node := parkedGate(t)
	rt.store.Append("ApprovalGranted", mid, map[string]any{"node_id": node.ID, "edit": nil})
	rt.store.Append("NodeResumed", mid, map[string]any{"node_id": node.ID})
	rt.Run(mid)
	if countEvents(rt, mid, "ApprovalInvalidated") != 0 {
		t.Fatal("a pre-digest approval is grandfathered")
	}
}

func TestStaleGateCannotBeApprovedWhenATTLIsConfiguredButCanBeRejected(t *testing.T) {
	t.Setenv(EnvApprovalTTL, "0.001")
	rt, mid, node := parkedGate(t)
	time.Sleep(20 * time.Millisecond)
	m, err := rt.ApproveBy(mid, node.ID, "approve", nil, "")
	if !errors.Is(err, ErrApprovalExpired) || m == nil {
		t.Fatalf("want ErrApprovalExpired, got %v", err)
	}
	if countEvents(rt, mid, "ApprovalExpired") != 1 || rt.repoState(t, mid) != MissionWaitingHuman {
		t.Fatal("expiry must be recorded and leave the mission waiting")
	}
	if countEvents(rt, mid, "ApprovalGranted") != 0 {
		t.Fatal("an expired approval must not be granted")
	}
	rt.Approve(mid, node.ID, "reject", nil)
	if rt.repoState(t, mid) != MissionFailed {
		t.Fatal("rejection is always allowed")
	}
}

func TestWithoutATTLAnOldGateCanStillBeApproved(t *testing.T) {
	t.Setenv(EnvApprovalTTL, "")
	rt, mid, node := parkedGate(t)
	time.Sleep(5 * time.Millisecond)
	if _, err := rt.ApproveBy(mid, node.ID, "approve", nil, ""); err != nil {
		t.Fatal(err)
	}
}
