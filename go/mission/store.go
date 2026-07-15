package mission

// Event-sourced stores — the source of truth the whole kernel agrees on. Everything is an
// append-only event; state is a fold over events. That buys a free audit trail, crash recovery
// (restart-resume) and replay for learning. Go port of agentic_os/mission/store.py.

import (
	"bufio"
	"encoding/json"
	"os"
	"sync"
)

// Event is one entry in the append-only log.
type Event struct {
	Type      string         `json:"type"`
	MissionID string         `json:"mission_id"`
	Payload   map[string]any `json:"payload"`
	Seq       int            `json:"seq"`
	Ts        float64        `json:"ts"`
}

// EventStore is an append-only log: an in-memory slice, optionally persisted to a JSONL file and
// reloaded on startup so a restart can rebuild every mission's state (the durability guarantee).
type EventStore struct {
	mu          sync.Mutex
	events      []*Event
	seq         int
	subscribers []func(*Event)
	path        string
}

// NewEventStore builds a store; if path is non-empty and exists, prior events are reloaded.
func NewEventStore(path string) *EventStore {
	s := &EventStore{path: path}
	if path != "" {
		if _, err := os.Stat(path); err == nil {
			s.load()
		}
	}
	return s
}

func (s *EventStore) load() {
	fh, err := os.Open(s.path)
	if err != nil {
		return
	}
	defer fh.Close()
	sc := bufio.NewScanner(fh)
	sc.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var ev Event
		if json.Unmarshal(line, &ev) != nil {
			continue
		}
		s.events = append(s.events, &ev)
		if ev.Seq > s.seq {
			s.seq = ev.Seq
		}
	}
}

// Append records an event. The payload is stored as passed — callers wrap struct/enum values with
// jsonable() so the log holds a clean, replayable JSON view (the Go analogue of to_jsonable).
func (s *EventStore) Append(typ, missionID string, payload map[string]any) *Event {
	s.mu.Lock()
	s.seq++
	ev := &Event{Type: typ, MissionID: missionID, Payload: payload, Seq: s.seq, Ts: now()}
	s.events = append(s.events, ev)
	if s.path != "" {
		if fh, err := os.OpenFile(s.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644); err == nil {
			b, _ := json.Marshal(ev)
			_, _ = fh.Write(append(b, '\n'))
			_ = fh.Close()
		}
	}
	subs := append([]func(*Event){}, s.subscribers...)
	s.mu.Unlock()
	for _, cb := range subs {
		func() {
			defer func() { _ = recover() }() // a subscriber must never break the log
			cb(ev)
		}()
	}
	return ev
}

func (s *EventStore) Subscribe(cb func(*Event)) {
	s.mu.Lock()
	s.subscribers = append(s.subscribers, cb)
	s.mu.Unlock()
}

// ForMission returns the events for one mission, in order.
func (s *EventStore) ForMission(missionID string) []*Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []*Event
	for _, e := range s.events {
		if e.MissionID == missionID {
			out = append(out, e)
		}
	}
	return out
}

func (s *EventStore) All() []*Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]*Event{}, s.events...)
}

// MissionIDs returns the distinct mission ids seen, in first-appearance order.
func (s *EventStore) MissionIDs() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	seen := map[string]bool{}
	var out []string
	for _, e := range s.events {
		if e.MissionID != "" && !seen[e.MissionID] {
			seen[e.MissionID] = true
			out = append(out, e.MissionID)
		}
	}
	return out
}

// ─── world state (the blackboard) ────────────────────────────────────────────

// FuseFunc fuses observations into one confidence-weighted belief (see belief.go).
type FuseFunc func([]map[string]any) Belief

// WorldState is an event-sourced blackboard for one mission. Capabilities read facts and write
// OBSERVATIONS; observations are fused into confidence-weighted BELIEFS before they become the fact
// a downstream capability reads.
type WorldState struct {
	MissionID string
	store     *EventStore
	fuse      FuseFunc
}

func NewWorldState(missionID string, store *EventStore, fuse FuseFunc) *WorldState {
	return &WorldState{MissionID: missionID, store: store, fuse: fuse}
}

func (w *WorldState) observations(key string) []map[string]any {
	var obs []map[string]any
	for _, e := range w.store.ForMission(w.MissionID) {
		if e.Type == "ObservationWritten" && e.Payload["key"] == key {
			obs = append(obs, e.Payload)
		}
	}
	return obs
}

func (w *WorldState) Observe(key string, value any, source string, confidence float64) {
	w.store.Append("ObservationWritten", w.MissionID, map[string]any{
		"key": key, "value": value, "source": source, "confidence": confidence,
	})
}

// Belief returns the fused belief for a key, or nil when there are no observations.
func (w *WorldState) Belief(key string) *Belief {
	obs := w.observations(key)
	if len(obs) == 0 {
		return nil
	}
	b := w.fuse(obs)
	return &b
}

// Get returns the fused value for a key (nil if unknown).
func (w *WorldState) Get(key string) any {
	if b := w.Belief(key); b != nil {
		return b.Value
	}
	return nil
}

// Snapshot returns the current belief for every observed key.
func (w *WorldState) Snapshot() map[string]*Belief {
	var keys []string
	seen := map[string]bool{}
	for _, e := range w.store.ForMission(w.MissionID) {
		if e.Type == "ObservationWritten" {
			if k, _ := e.Payload["key"].(string); k != "" && !seen[k] {
				seen[k] = true
				keys = append(keys, k)
			}
		}
	}
	out := make(map[string]*Belief, len(keys))
	for _, k := range keys {
		out[k] = w.Belief(k)
	}
	return out
}

// ─── repositories (fold events → current state) ──────────────────────────────

type MissionRepository struct{ store *EventStore }

func NewMissionRepository(store *EventStore) *MissionRepository { return &MissionRepository{store} }

// State folds the mission's state (nil if the mission is unknown).
func (r *MissionRepository) State(missionID string) (MissionState, bool) {
	var st MissionState
	found := false
	for _, e := range r.store.ForMission(missionID) {
		switch e.Type {
		case "MissionCreated":
			st, found = MissionPlanning, true
		case "MissionStateChanged":
			if s, ok := e.Payload["state"].(string); ok {
				st, found = MissionState(s), true
			}
		}
	}
	return st, found
}

func (r *MissionRepository) ActivePlanID(missionID string) string {
	pid := ""
	for _, e := range r.store.ForMission(missionID) {
		if e.Type == "PlanActivated" {
			pid, _ = e.Payload["plan_id"].(string)
		}
	}
	return pid
}

func (r *MissionRepository) Goal(missionID string) string {
	for _, e := range r.store.ForMission(missionID) {
		if e.Type == "MissionCreated" {
			g, _ := e.Payload["goal"].(string)
			return g
		}
	}
	return ""
}

var nodeEventState = map[string]NodeState{
	"NodeDispatched":  NodeRunning,
	"NodeSucceeded":   NodeDone,
	"NodeFailed":      NodeFailed,
	"NodeParked":      NodeWaiting,
	"NodeResumed":     NodeRunning,
	"NodeCompensated": NodeCompensated,
	"NodeSkipped":     NodeSkipped,
}

// NodeStatus folds the current status of every node the mission has touched.
func (r *MissionRepository) NodeStatus(missionID string) map[string]NodeState {
	status := map[string]NodeState{}
	for _, e := range r.store.ForMission(missionID) {
		if st, ok := nodeEventState[e.Type]; ok {
			if nid, ok := e.Payload["node_id"].(string); ok {
				status[nid] = st
			}
		}
	}
	return status
}

func (r *MissionRepository) NodeResults(missionID string) map[string]map[string]any {
	out := map[string]map[string]any{}
	for _, e := range r.store.ForMission(missionID) {
		if e.Type == "NodeSucceeded" {
			nid, _ := e.Payload["node_id"].(string)
			res, _ := e.Payload["result"].(map[string]any)
			if res == nil {
				res = map[string]any{}
			}
			out[nid] = res
		}
	}
	return out
}

// PendingHuman returns the open human task (parked minus resolved), or nil. Insertion order is
// preserved so the first still-open park is returned, matching the Python fold.
func (r *MissionRepository) PendingHuman(missionID string) map[string]any {
	parked := map[string]map[string]any{}
	var order []string
	for _, e := range r.store.ForMission(missionID) {
		switch e.Type {
		case "NodeParked":
			nid, _ := e.Payload["node_id"].(string)
			task := map[string]any{"capability": e.Payload["capability"]}
			if ht, ok := e.Payload["human_task"].(map[string]any); ok {
				for k, v := range ht {
					task[k] = v
				}
			}
			if _, seen := parked[nid]; !seen {
				order = append(order, nid)
			}
			parked[nid] = task
		case "NodeResumed", "NodeSkipped":
			nid, _ := e.Payload["node_id"].(string)
			delete(parked, nid)
		}
	}
	for _, nid := range order {
		if task, ok := parked[nid]; ok {
			out := map[string]any{"node_id": nid}
			for k, v := range task {
				out[k] = v
			}
			return out
		}
	}
	return nil
}

func (r *MissionRepository) Timeline(missionID string) []map[string]any {
	var out []map[string]any
	for _, e := range r.store.ForMission(missionID) {
		out = append(out, map[string]any{"seq": e.Seq, "ts": e.Ts, "type": e.Type, "payload": e.Payload})
	}
	return out
}

func (r *MissionRepository) Created(missionID string) map[string]any {
	for _, e := range r.store.ForMission(missionID) {
		if e.Type == "MissionCreated" {
			return e.Payload
		}
	}
	return map[string]any{}
}

// ListMissions returns every mission the store knows about (durable across restarts).
func (r *MissionRepository) ListMissions() []map[string]any {
	var out []map[string]any
	for _, mid := range r.store.MissionIDs() {
		c := r.Created(mid)
		row := map[string]any{"id": mid, "goal": c["goal"], "template": c["template"], "state": nil}
		if st, ok := r.State(mid); ok {
			row["state"] = string(st)
		}
		out = append(out, row)
	}
	return out
}
