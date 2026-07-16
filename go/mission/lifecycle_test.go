package mission

import "testing"

func TestLifecycleDispatchDeliversDataAndCapabilities(t *testing.T) {
	var got []string
	reg := NewLifecycleRegistry(map[string]any{"svc": "evidence-log"}).Install(LifecycleContributor{
		OnMissionStarted: func(e MissionStarted, caps map[string]any) {
			got = append(got, e.MissionID+":"+e.Goal+":"+caps["svc"].(string))
		},
	})
	reg.Dispatch(MissionStarted{MissionID: "m1", Goal: "deploy", Template: "deploy_app"})
	if len(got) != 1 || got[0] != "m1:deploy:evidence-log" {
		t.Fatalf("got %v", got)
	}
}

func TestLifecyclePanicNeverBreaksLoop(t *testing.T) {
	reg := NewLifecycleRegistry(nil).Install(LifecycleContributor{
		OnMissionFinished: func(e MissionFinished, caps map[string]any) { panic("boom") },
	})
	reg.Dispatch(MissionFinished{MissionID: "m1", State: "succeeded"}) // must not panic
}

func TestLifecycleEmptyIsNoop(t *testing.T) {
	NewLifecycleRegistry(nil).Dispatch(SessionIdle{}) // no contributors → no-op
}
