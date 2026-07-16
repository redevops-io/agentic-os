package mission

// Mission lifecycle contributors (Go port of lifecycle.py). A contributor receives data-only inputs
// at dispatch time, acts only through capabilities injected at install time, and never owns loop
// control. Optional + no-op when empty; a contributor that panics is recovered (an extension must
// never break the mission loop).

// ── data-only lifecycle events ──
type MissionStarted struct {
	MissionID string
	Goal      string
	Template  string
}
type NodeCompleted struct {
	MissionID  string
	NodeID     string
	Capability string
	Status     string
}
type GateReached struct {
	MissionID  string
	NodeID     string
	Capability string
}
type MissionFinished struct {
	MissionID string
	State     string // succeeded | failed
}
type SessionIdle struct {
	Reason string
}

// LifecycleContributor — set the hooks you care about; nil hooks are simply not called. Each
// receives the event + the injected capabilities dict (the data-services it may act through).
type LifecycleContributor struct {
	OnMissionStarted  func(MissionStarted, map[string]any)
	OnNodeCompleted   func(NodeCompleted, map[string]any)
	OnGateReached     func(GateReached, map[string]any)
	OnMissionFinished func(MissionFinished, map[string]any)
	OnSessionIdle     func(SessionIdle, map[string]any)
}

// LifecycleRegistry holds contributors + injected capabilities and dispatches events. No-op when empty.
type LifecycleRegistry struct {
	contributors []LifecycleContributor
	Capabilities map[string]any
}

func NewLifecycleRegistry(capabilities map[string]any) *LifecycleRegistry {
	if capabilities == nil {
		capabilities = map[string]any{}
	}
	return &LifecycleRegistry{Capabilities: capabilities}
}

func (r *LifecycleRegistry) Install(c LifecycleContributor) *LifecycleRegistry {
	r.contributors = append(r.contributors, c)
	return r
}

func (r *LifecycleRegistry) Inject(name string, capability any) *LifecycleRegistry {
	r.Capabilities[name] = capability
	return r
}

// Dispatch delivers a data-only event to every contributor's matching hook (return ignored — no
// loop control). A panicking contributor is recovered so orchestration is never broken.
func (r *LifecycleRegistry) Dispatch(event any) {
	for _, c := range r.contributors {
		func(c LifecycleContributor) {
			defer func() { _ = recover() }()
			switch e := event.(type) {
			case MissionStarted:
				if c.OnMissionStarted != nil {
					c.OnMissionStarted(e, r.Capabilities)
				}
			case NodeCompleted:
				if c.OnNodeCompleted != nil {
					c.OnNodeCompleted(e, r.Capabilities)
				}
			case GateReached:
				if c.OnGateReached != nil {
					c.OnGateReached(e, r.Capabilities)
				}
			case MissionFinished:
				if c.OnMissionFinished != nil {
					c.OnMissionFinished(e, r.Capabilities)
				}
			case SessionIdle:
				if c.OnSessionIdle != nil {
					c.OnSessionIdle(e, r.Capabilities)
				}
			}
		}(c)
	}
}
