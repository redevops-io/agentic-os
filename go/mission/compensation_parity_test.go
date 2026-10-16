package mission

import (
	"encoding/json"
	"os"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// Shared Python<->Go fixtures produced by scripts/gen_parity_fixtures.py from the REAL Python runtime
// (tests/test_go_parity_fixtures.py regenerates and compares them). Each scenario is replayed through the Go runtime;
// after the run and after every operator step the normalised state, outcome, compensation events, handler calls and
// attention items must be identical.

type specJSON struct {
	Name                  string            `json:"name"`
	Provides              *string           `json:"provides"`
	SideEffecting         bool              `json:"side_effecting"`
	Undo                  string            `json:"undo"`
	Compensation          *CompensationSpec `json:"compensation"`
	Irreversible          bool              `json:"irreversible"`
	IrreversibleNote      string            `json:"irreversible_note"`
	MissionOwnedTemporary bool              `json:"mission_owned_temporary"`
	ApprovalRequired      bool              `json:"approval_required"`
}

func (s specJSON) cap() CapabilitySpec {
	c := NewCapabilitySpec(s.Name, strings.SplitN(s.Name, ".", 2)[0])
	c.Permissions = []string{"*"}
	if s.Provides != nil {
		c.Provides = []string{*s.Provides}
	}
	c.SideEffecting, c.Undo, c.Compensation = s.SideEffecting, s.Undo, s.Compensation
	c.Irreversible, c.IrreversibleNote = s.Irreversible, s.IrreversibleNote
	c.MissionOwnedTemporary, c.ApprovalRequired = s.MissionOwnedTemporary, s.ApprovalRequired
	return c
}

type handlerJSON struct {
	Result map[string]any `json:"result"`
	Error  string         `json:"error"`
	Seq    []handlerJSON  `json:"seq"`
}

type stepJSON struct {
	Set       map[string]handlerJSON `json:"set"`
	Intervene *struct {
		Action, Actor, Reason, Node string
	} `json:"intervene"`
}

type scenarioJSON struct {
	Name     string                 `json:"name"`
	Specs    []specJSON             `json:"specs"`
	Handlers map[string]handlerJSON `json:"handlers"`
	Outcomes []string               `json:"outcomes"`
	Mission  struct {
		Template      *string `json:"template"`
		FailurePolicy string  `json:"failure_policy"`
	} `json:"mission"`
	Env      map[string]string `json:"env"`
	Steps    []stepJSON        `json:"steps"`
	Expected []map[string]any  `json:"expected"`
}

type chainPlanner struct{ outcomes []string }

func (p chainPlanner) Plan(missionID, goal string, _ map[string]any) ExecutionIntent {
	var steps []IntentStep
	var prev []string
	for _, o := range p.outcomes {
		st := NewIntentStep(o, "do "+o)
		st.InputsFrom = append([]string{}, prev...)
		steps = append(steps, st)
		prev = []string{o}
	}
	return NewExecutionIntent(missionID, steps...)
}

func mkHandler(table map[string]handlerJSON, name string, calls *[]string) Handler {
	i := 0
	return func(map[string]any) (map[string]any, error) {
		*calls = append(*calls, name)
		h := table[name]
		if h.Seq != nil {
			idx := i
			if idx >= len(h.Seq) {
				idx = len(h.Seq) - 1
			}
			i++
			h = h.Seq[idx]
		}
		if h.Error != "" {
			return nil, &OperatorError{h.Error}
		}
		b, _ := json.Marshal(h.Result)
		var out map[string]any
		_ = json.Unmarshal(b, &out)
		return out, nil
	}
}

var eventsOfInterest = setOf("CompensationRequested", "CompensationAuthorized", "CompensationExecuted", "CompensationVerified",
	"CompensationFailed", "CompensationApprovalRequired", "CompensationApproved", "CompensationAcknowledged",
	"CompensationUndeclaredTarget", "CompensationSkipped", "CompensationRetryRequested", "EffectsRetained",
	"NodeCompensated", "NodeCompensationFailed", "LegacyFailurePolicyNotice", "DefaultPolicyReresolved",
	"MissionBlocked", "NodeFailed")

// normalize: JSON round-trip, replace volatile ids, drop wall-clock fields.
func normalizeJSON(v any, table map[string]string, mid string) any {
	b, _ := json.Marshal(v)
	var g any
	_ = json.Unmarshal(b, &g)
	olds := make([]string, 0, len(table))
	for o := range table {
		olds = append(olds, o)
	}
	sort.Slice(olds, func(i, j int) bool { return len(olds[i]) > len(olds[j]) })
	var walk func(x any) any
	str := func(t string) string {
		for _, o := range olds {
			t = strings.ReplaceAll(t, o, table[o])
		}
		return strings.ReplaceAll(t, mid, "MID")
	}
	walk = func(x any) any {
		switch t := x.(type) {
		case map[string]any:
			out := map[string]any{}
			for k, e := range t {
				if k == "at" || k == "exp" || k == "ts" || k == "nonce" {
					continue
				}
				out[str(k)] = walk(e)
			}
			return out
		case []any:
			out := make([]any, len(t))
			for i, e := range t {
				out[i] = walk(e)
			}
			return out
		case string:
			return str(t)
		}
		return x
	}
	return walk(g)
}

func approveGates(rt *MissionRuntime, m *Mission) {
	for i := 0; i < 20 && m.State == MissionWaitingHuman; i++ {
		rt.Approve(m.ID, rt.repo.PendingHuman(m.ID)["node_id"].(string), "approve", nil)
	}
}

func TestCompensationScenariosMatchPython(t *testing.T) {
	raw, err := os.ReadFile("testdata/compensation_scenarios.json")
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Scenarios []scenarioJSON `json:"scenarios"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Scenarios) < 20 {
		t.Fatalf("fixture looks truncated: %d scenarios", len(doc.Scenarios))
	}
	for _, sc := range doc.Scenarios {
		sc := sc
		t.Run(sc.Name, func(t *testing.T) {
			t.Setenv(EnvDefaultFailurePolicy, "")
			os.Unsetenv(EnvDefaultFailurePolicy)
			for k, v := range sc.Env {
				t.Setenv(k, v)
			}
			reg := NewCapabilityRegistry(nil)
			var caps []CapabilitySpec
			for _, s := range sc.Specs {
				caps = append(caps, s.cap())
			}
			if err := reg.Register(CapabilityManifest{Operator: "m", Capabilities: caps}); err != nil {
				t.Fatal(err)
			}
			var calls []string
			table := map[string]handlerJSON{}
			hs := map[string]Handler{}
			for n, h := range sc.Handlers {
				table[n] = h
			}
			for n := range sc.Handlers {
				hs[n] = mkHandler(table, n, &calls)
			}
			client := NewInMemoryOperatorClient(hs)
			rt := NewMissionRuntime(reg, NewExecutor(client), WithPlanner(chainPlanner{sc.Outcomes}))
			opts := CreateOpts{PolicyRefs: []string{"*"}, FailurePolicy: sc.Mission.FailurePolicy}
			if sc.Mission.Template != nil {
				opts.Template = *sc.Mission.Template
			}
			m := rt.CreateMission("go", opts)
			plan := rt.plans[m.ID]
			names := map[string]string{}
			byCap := map[string]string{}
			for _, n := range plan.Graph.Nodes {
				names[n.ID] = "node:" + n.Capability
				names[n.IdempotencyKey] = "KEY:" + n.Capability
				byCap[n.Capability] = n.ID
			}
			rt.Run(m.ID)
			approveGates(rt, m)

			snap := func(label string) map[string]any {
				var evs []any
				for _, e := range rt.store.ForMission(m.ID) {
					if eventsOfInterest[e.Type] {
						evs = append(evs, map[string]any{"type": e.Type, "payload": e.Payload})
					}
				}
				attn := []any{}
				for _, a := range rt.AttentionItems(m.ID) {
					attn = append(attn, a)
				}
				if evs == nil {
					evs = []any{}
				}
				cl := append([]string{}, calls...)
				if cl == nil {
					cl = []string{}
				}
				return normalizeJSON(map[string]any{"label": label, "state": string(m.State), "outcome": m.Outcome,
					"events": evs, "calls": cl, "attention": attn}, names, m.ID).(map[string]any)
			}
			got := []map[string]any{snap("run")}
			for i, st := range sc.Steps {
				if st.Set != nil {
					for n, h := range st.Set {
						table[n] = h
						client.handlers[n] = mkHandler(table, n, &calls)
					}
					got = append(got, snap("step"+itoa(i)+":set"))
				}
				if st.Intervene != nil {
					iv := st.Intervene
					_, err := rt.CompensationIntervention(m.ID, iv.Action, iv.Actor, iv.Reason, byCap[iv.Node])
					s := snap("step" + itoa(i) + ":" + iv.Action)
					msg := ""
					if err != nil {
						msg = err.Error()
					}
					s["error"] = msg
					got = append(got, s)
				}
			}
			// fold/rehydrate equality: a fresh runtime on the same log reports the same state and compensation outcome
			rt2 := NewMissionRuntime(reg, NewExecutor(client), WithStore(rt.store), WithPlanner(chainPlanner{sc.Outcomes}))
			m2, err := rt2.RehydrateStrict(m.ID, RehydrateOpts{})
			if err != nil {
				t.Fatal(err)
			}
			pick := func(o map[string]any) map[string]any {
				out := map[string]any{}
				for k, v := range o {
					if OutcomeKeys[k] {
						out[k] = v
					}
				}
				return normalizeJSON(out, names, m.ID).(map[string]any)
			}
			if m2.State != m.State || !reflect.DeepEqual(pick(m2.Outcome), pick(m.Outcome)) {
				a, _ := json.MarshalIndent(pick(m.Outcome), "", " ")
				b, _ := json.MarshalIndent(pick(m2.Outcome), "", " ")
				t.Errorf("rehydrated mission differs from live (state %s vs %s)\n--- live\n%s\n--- rehydrated\n%s", m.State, m2.State, a, b)
			}
			if len(got) != len(sc.Expected) {
				t.Fatalf("snapshots: go=%d python=%d", len(got), len(sc.Expected))
			}
			for i := range got {
				want := sc.Expected[i]
				if !reflect.DeepEqual(got[i], want) {
					for _, k := range []string{"state", "calls", "outcome", "events", "attention", "error"} {
						if !reflect.DeepEqual(got[i][k], want[k]) {
							gb, _ := json.MarshalIndent(got[i][k], "", " ")
							wb, _ := json.MarshalIndent(want[k], "", " ")
							t.Errorf("%s [%s] %s differs\n--- go\n%s\n--- python\n%s", sc.Name, got[i]["label"], k, gb, wb)
						}
					}
				}
			}
		})
	}
}

func TestCompensationRegistrationMatchesPython(t *testing.T) {
	raw, err := os.ReadFile("testdata/compensation_registration.json")
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Cases []struct {
			Name  string            `json:"name"`
			Specs []json.RawMessage `json:"specs"`
			Error string            `json:"error"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	for _, c := range doc.Cases {
		var caps []CapabilitySpec
		var rerr error
		for _, rs := range c.Specs {
			var s specJSON
			if err := json.Unmarshal(rs, &s); err != nil {
				rerr = err
				break
			}
			caps = append(caps, s.cap())
		}
		if rerr == nil {
			rerr = NewCapabilityRegistry(nil).Register(CapabilityManifest{Operator: "m", Capabilities: caps})
		}
		got := ""
		if rerr != nil {
			got = rerr.Error()
		}
		if strings.HasPrefix(c.Error, "unknown compensation field(s)") { // the offending-field list is formatted per language
			if !strings.HasPrefix(got, "unknown compensation field(s)") && !regexp.MustCompile(`unknown compensation field`).MatchString(got) {
				t.Errorf("%s: go=%q python=%q", c.Name, got, c.Error)
			}
			continue
		}
		if got != c.Error {
			t.Errorf("%s: go=%q python=%q", c.Name, got, c.Error)
		}
	}
}

func TestResultClassifierMatchesPython(t *testing.T) {
	raw, err := os.ReadFile("testdata/result_classification.json")
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Cases []struct {
			Result         map[string]any `json:"result"`
			Failed         bool           `json:"failed"`
			Reason         string         `json:"reason"`
			Staged         bool           `json:"staged"`
			Proves         bool           `json:"proves_execution"`
			FailedNoOutput bool           `json:"failed_side_effect_no_outputs"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	for _, c := range doc.Cases {
		failed, why := ResultFailed(c.Result, false, true)
		if failed != c.Failed || why != c.Reason || IsStaged(c.Result) != c.Staged || ProvesExecution(c.Result) != c.Proves {
			t.Errorf("%v: go=(%v,%q,%v,%v) python=(%v,%q,%v,%v)", c.Result, failed, why, IsStaged(c.Result), ProvesExecution(c.Result),
				c.Failed, c.Reason, c.Staged, c.Proves)
		}
		if f, _ := ResultFailed(c.Result, true, false); f != c.FailedNoOutput {
			t.Errorf("%v: side-effecting/no-outputs go=%v python=%v", c.Result, f, c.FailedNoOutput)
		}
	}
}

func TestCompensationFoldMatchesPython(t *testing.T) {
	raw, err := os.ReadFile("testdata/compensation_fold.json")
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Cases []struct {
			Name   string                    `json:"name"`
			Events []Event                   `json:"events"`
			Folded map[string]map[string]any `json:"folded"`
			Sum    map[string]any            `json:"summary"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	str := func(v any) string { s, _ := v.(string); return s }
	for _, c := range doc.Cases {
		var evs []*Event
		for i := range c.Events {
			evs = append(evs, &c.Events[i])
		}
		got := FoldCompensation(evs)
		if len(got) != len(c.Folded) {
			t.Errorf("%s: nodes go=%d python=%d", c.Name, len(got), len(c.Folded))
		}
		for nid, want := range c.Folded {
			g := got[nid]
			if g == nil {
				t.Errorf("%s: node %s missing", c.Name, nid)
				continue
			}
			if g.Status != want["status"] || g.Undo != str(want["undo"]) || g.Reason != str(want["reason"]) ||
				float64(g.Attempts) != want["attempts"] || float64(g.Attempt) != want["attempt"] || g.Method != str(want["method"]) {
				t.Errorf("%s/%s: go=%+v python=%v", c.Name, nid, *g, want)
			}
			if (g.Approval == nil) != (want["approval"] == nil) ||
				(g.Approval != nil && str(g.Approval["approver"]) != str(want["approval"].(map[string]any)["approver"])) {
				t.Errorf("%s/%s: approval go=%v python=%v", c.Name, nid, g.Approval, want["approval"])
			}
			for k, have := range map[string]string{"inputs_sha256": g.InputsSHA256, "actor": g.Actor, "ack_reason": g.AckReason} {
				if w, ok := want[k]; ok && str(w) != have {
					t.Errorf("%s/%s: %s go=%q python=%v", c.Name, nid, k, have, w)
				}
			}
		}
		s := SummarizeCompensation(got)
		lst := func(v any) []string {
			var o []string
			for _, x := range v.([]any) {
				o = append(o, x.(string))
			}
			return o
		}
		mp := func(v any) map[string]string {
			o := map[string]string{}
			for k, x := range v.(map[string]any) {
				o[k] = x.(string)
			}
			return o
		}
		if !reflect.DeepEqual(append([]string{}, s.Compensated...), append([]string{}, lst(c.Sum["compensated"])...)) ||
			!reflect.DeepEqual(append([]string{}, s.CompensatedUnverified...), append([]string{}, lst(c.Sum["compensated_unverified"])...)) ||
			!reflect.DeepEqual(append([]string{}, s.CompensationAck...), append([]string{}, lst(c.Sum["compensation_acknowledged"])...)) ||
			!reflect.DeepEqual(s.CompensationFailed, mp(c.Sum["compensation_failed"])) ||
			!reflect.DeepEqual(s.CompensationSkipped, mp(c.Sum["compensation_skipped"])) || s.ReadbackOnly != c.Sum["readback_only"] {
			t.Errorf("%s: summary go=%+v python=%v", c.Name, s, c.Sum)
		}
	}
}
