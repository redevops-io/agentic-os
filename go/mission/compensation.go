package mission

// Saga-style compensation contract (docs/COMPENSATION.md): vocabulary, capability metadata, registration checks
// and the log fold. Go port of agentic_os/mission/compensation.py. NOT automatic rollback: EXPLICIT_ONLY.
// A consequential capability DECLARES whether it supports compensation; nothing is ever assumed reversible.

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
)

const CompensationMode = "EXPLICIT_ONLY"

// Events appended to the durable mission log (every payload carries node_id).
const (
	EvCompRequested        = "CompensationRequested"
	EvCompAuthorized       = "CompensationAuthorized"
	EvCompExecuted         = "CompensationExecuted"
	EvCompVerified         = "CompensationVerified"
	EvCompFailed           = "CompensationFailed"
	EvCompApprovalRequired = "CompensationApprovalRequired"
	EvCompApproved         = "CompensationApproved"
	EvCompAcknowledged     = "CompensationAcknowledged"
	EvCompUndeclaredTarget = "CompensationUndeclaredTarget"
	EvCompSkipped          = "CompensationSkipped"
	EvEffectsRetained      = "EffectsRetained"
)

var compensationEvents = map[string]bool{EvCompRequested: true, EvCompAuthorized: true, EvCompExecuted: true,
	EvCompVerified: true, EvCompFailed: true, EvCompApprovalRequired: true, EvCompApproved: true,
	EvCompAcknowledged: true, EvCompUndeclaredTarget: true, EvCompSkipped: true}

// Per-action dispositions. "" == COMPENSATE. PRESERVE: the effect is KEPT on failure whatever the policy.
// CLEANUP: a TEMPORARY mission-owned resource cleaned up even under PRESERVE_ONLY (fully guarded declaration).
const (
	DispPreserve = "PRESERVE"
	DispCleanup  = "CLEANUP"
	AuthHuman    = "HUMAN_APPROVAL"
	AuthEvidence = "EVIDENCE_BOUND"

	EnvCompMaxAttempts = "AGENTIC_OS_COMPENSATION_MAX_ATTEMPTS"
	EnvStrictTargets   = "AGENTIC_OS_STRICT_UNDO_TARGETS"
)

func compMaxAttempts() int {
	n, err := strconv.Atoi(strings.TrimSpace(os.Getenv(EnvCompMaxAttempts)))
	if os.Getenv(EnvCompMaxAttempts) == "" || err != nil {
		return 3
	}
	if n < 1 {
		return 1
	}
	return n
}

// Reversibility classes.
const (
	ReversibleExplicit = "REVERSIBLE_EXPLICIT"
	Irreversible       = "IRREVERSIBLE"
	ReversibilityUnk   = "UNKNOWN"
	NotApplicable      = "NOT_APPLICABLE"
)

// CompensationSpec: compensation: {supported, capability, reversibility, requires_approval, verify, notes, ...}.
type CompensationSpec struct {
	Supported           bool   `json:"supported"`
	Capability          string `json:"capability"`
	Reversibility       string `json:"reversibility"`
	RequiresApproval    bool   `json:"requires_approval"`
	Verify              string `json:"verify"`
	Notes               string `json:"notes"`
	Disposition         string `json:"disposition"`
	Preconditions       string `json:"preconditions"`
	AuthorizationPolicy string `json:"authorization_policy"`
	IncursOngoingCost   bool   `json:"incurs_ongoing_cost"`
}

// UnmarshalJSON rejects unknown fields (fail closed), like Python coerce().
func (s *CompensationSpec) UnmarshalJSON(b []byte) error {
	type plain CompensationSpec
	var p plain
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&p); err != nil {
		if strings.Contains(err.Error(), "unknown field") {
			return fmt.Errorf("unknown compensation field(s): %s", strings.TrimPrefix(err.Error(), "json: unknown field "))
		}
		return err
	}
	*s = CompensationSpec(p)
	return nil
}

func pyq(s string) string { return "'" + s + "'" }

// Coerce validates and normalises a spec exactly like Python compensation.coerce (returns a normalised copy).
func (in *CompensationSpec) Coerce() (*CompensationSpec, error) {
	if in == nil {
		return nil, nil
	}
	spec := *in
	known := map[string]bool{"": true, "COMPENSATE": true, DispPreserve: true, DispCleanup: true}
	switch {
	case spec.Reversibility != "" && spec.Reversibility != ReversibleExplicit && spec.Reversibility != Irreversible &&
		spec.Reversibility != ReversibilityUnk && spec.Reversibility != NotApplicable:
		return nil, fmt.Errorf("unknown reversibility %s", pyq(spec.Reversibility))
	case spec.Supported && spec.Capability == "":
		return nil, fmt.Errorf("compensation.supported requires compensation.capability")
	case !spec.Supported && spec.Reversibility == ReversibleExplicit:
		return nil, fmt.Errorf("compensation.supported=False contradicts reversibility=REVERSIBLE_EXPLICIT")
	case !spec.Supported && spec.Capability != "":
		return nil, fmt.Errorf("compensation.supported=False must not name a compensating capability")
	case spec.Supported && spec.Reversibility != "" && spec.Reversibility != ReversibleExplicit:
		return nil, fmt.Errorf("compensation.supported=True requires reversibility REVERSIBLE_EXPLICIT")
	case !known[spec.Disposition]:
		return nil, fmt.Errorf("unknown compensation disposition %s", pyq(spec.Disposition))
	case spec.Disposition == DispPreserve && spec.Supported:
		return nil, fmt.Errorf("compensation.disposition=PRESERVE contradicts supported=True: a preserved effect is " +
			"never automatically reversed, so declare supported=False (reversal is a new, " +
			"separately authorized decision)")
	case spec.Preconditions != "" && !spec.Supported:
		return nil, fmt.Errorf("compensation.preconditions requires supported=True")
	}
	if spec.Disposition == DispCleanup {
		switch {
		case !spec.Supported:
			return nil, fmt.Errorf("compensation.disposition=CLEANUP requires supported=True and a compensating capability")
		case spec.Preconditions == "":
			return nil, fmt.Errorf("compensation.disposition=CLEANUP requires `preconditions` (a read-only check that " +
				"the cleanup is independently safe NOW)")
		case spec.AuthorizationPolicy == "" && !spec.RequiresApproval:
			return nil, fmt.Errorf("compensation.disposition=CLEANUP requires an explicit authorization_policy " +
				"(HUMAN_APPROVAL, or EVIDENCE_BOUND when the UNDO capability is registered " +
				"mission_owned_temporary=True; enforced at registration)")
		case spec.IncursOngoingCost:
			return nil, fmt.Errorf("compensation.disposition=CLEANUP is for temporary resources: a cost-bearing " +
				"(incurs_ongoing_cost) effect is retained and reported, never auto-cleaned")
		}
	}
	switch {
	case spec.AuthorizationPolicy != "" && spec.AuthorizationPolicy != AuthHuman && spec.AuthorizationPolicy != AuthEvidence:
		return nil, fmt.Errorf("unknown compensation authorization_policy %s", pyq(spec.AuthorizationPolicy))
	case spec.AuthorizationPolicy == AuthEvidence && spec.RequiresApproval:
		return nil, fmt.Errorf("compensation.authorization_policy=EVIDENCE_BOUND contradicts requires_approval=True")
	}
	if spec.AuthorizationPolicy == AuthHuman {
		spec.RequiresApproval = true
	}
	return &spec, nil
}

// compSpec returns the capability's normalised spec, or nil (none declared / invalid -> treated as undeclared).
func compSpec(c *CapabilitySpec) *CompensationSpec {
	if c == nil || c.Compensation == nil {
		return nil
	}
	s, err := c.Compensation.Coerce()
	if err != nil {
		return nil
	}
	return s
}

func isPreserved(c *CapabilitySpec) bool {
	s := compSpec(c)
	return s != nil && s.Disposition == DispPreserve
}
func isCleanup(c *CapabilitySpec) bool {
	s := compSpec(c)
	return s != nil && s.Disposition == DispCleanup && s.Supported
}
func incursOngoingCost(c *CapabilitySpec) bool {
	s := compSpec(c)
	return s != nil && s.IncursOngoingCost
}

// declaredUndo: the compensating capability a capability DECLARES (explicit spec first, then legacy `undo`).
func declaredUndo(c *CapabilitySpec) string {
	if c == nil {
		return ""
	}
	s := compSpec(c)
	if s != nil && s.Supported && s.Capability != "" {
		return s.Capability
	}
	if s != nil && s.Disposition == DispPreserve {
		return "" // a PRESERVE declaration wins over a stray legacy undo=
	}
	return c.Undo
}

func reversibilityOf(c *CapabilitySpec) string {
	if c == nil || !c.SideEffecting {
		return NotApplicable
	}
	if declaredUndo(c) != "" {
		return ReversibleExplicit
	}
	s := compSpec(c)
	if c.Irreversible || (s != nil && !s.Supported && s.Reversibility != ReversibilityUnk) {
		return Irreversible
	}
	return ReversibilityUnk
}

func irreversibleNote(c *CapabilitySpec) string {
	n := ""
	if c != nil {
		n = c.IrreversibleNote
	}
	if n == "" {
		if s := compSpec(c); s != nil {
			n = s.Notes
		}
	}
	return strings.TrimSpace(n)
}

// intrinsicProblems: registration-time checks that need no other capability.
func intrinsicProblems(c *CapabilitySpec) []string {
	var out []string
	name := c.Name
	spec := compSpec(c)
	undo := declaredUndo(c)
	if undo != "" && undo == name {
		out = append(out, name+": compensation target is the capability itself (would re-run the forward action)")
	}
	if spec != nil && spec.Disposition == DispPreserve && c.Undo != "" {
		out = append(out, fmt.Sprintf("%s: declares disposition=PRESERVE and a legacy undo=%s (contradiction)", name, pyq(c.Undo)))
	}
	if spec != nil && spec.Preconditions != "" && (spec.Preconditions == name || spec.Preconditions == undo || spec.Preconditions == spec.Verify) {
		out = append(out, fmt.Sprintf("%s: preconditions '%s' must be a read-only check distinct from the "+
			"forward action, the compensating capability and the verify read-back", name, spec.Preconditions))
	}
	if spec != nil && spec.Verify != "" && (spec.Verify == name || spec.Verify == undo) {
		out = append(out, fmt.Sprintf("%s: verify '%s' must be a read-back distinct from the forward action and "+
			"the compensating capability (verify == undo would run the undo twice)", name, spec.Verify))
	}
	if c.MissionOwnedTemporary {
		ongoing := spec != nil && spec.IncursOngoingCost
		if c.ApprovalRequired || ongoing || reversibilityOf(c) == ReversibleExplicit {
			out = append(out, name+": mission_owned_temporary is for the non-consequential undo of a temporary artifact: an "+
				"approval-gated, cost-bearing or itself-compensatable capability can never be marked temporary")
		}
	}
	if spec != nil && spec.Supported && c.Irreversible {
		out = append(out, name+": declares both compensation.supported and irreversible")
	}
	if c.Undo != "" && spec != nil && spec.Supported && c.Undo != spec.Capability {
		out = append(out, fmt.Sprintf("%s: undo=%s disagrees with compensation.capability=%s", name, pyq(c.Undo), pyq(spec.Capability)))
	}
	return out
}

// cleanupWithoutHumanProblems: a CLEANUP may run WITHOUT a human only when ALL hold (empty = valid).
func cleanupWithoutHumanProblems(c *CapabilitySpec, get func(string) *CapabilitySpec) []string {
	spec := compSpec(c)
	if spec == nil || !spec.Supported || spec.Disposition != DispCleanup {
		return nil
	}
	if spec.RequiresApproval || spec.AuthorizationPolicy == AuthHuman {
		return nil
	}
	var out []string
	name := c.Name
	if c.ApprovalRequired || spec.IncursOngoingCost {
		out = append(out, name+": a consequential (approval-gated / cost-bearing) capability needs HUMAN_APPROVAL for its CLEANUP")
	}
	t := get(spec.Capability)
	switch {
	case t == nil:
		out = append(out, fmt.Sprintf("%s: CLEANUP without a human needs its undo '%s' registered with mission_owned_temporary=True", name, spec.Capability))
	case !t.MissionOwnedTemporary:
		out = append(out, fmt.Sprintf("%s: CLEANUP without a human requires the undo '%s' to be registered with "+
			"mission_owned_temporary=True (otherwise declare HUMAN_APPROVAL)", name, spec.Capability))
	case t.ApprovalRequired || incursOngoingCost(t):
		out = append(out, fmt.Sprintf("%s: undo '%s' is approval-gated or cost-bearing: it can never be a no-human cleanup", name, spec.Capability))
	}
	return out
}

func evidenceBoundConflicts(caps []*CapabilitySpec, get func(string) *CapabilitySpec) []string {
	var out []string
	for _, c := range caps {
		out = append(out, cleanupWithoutHumanProblems(c, get)...)
	}
	for _, c := range caps {
		spec := compSpec(c)
		if spec == nil || !spec.Supported || spec.AuthorizationPolicy != AuthEvidence {
			continue
		}
		if t := get(spec.Capability); t != nil && t.ApprovalRequired {
			out = append(out, fmt.Sprintf("%s: authorization_policy=EVIDENCE_BOUND but its compensating capability "+
				"'%s' requires approval (declare HUMAN_APPROVAL)", c.Name, spec.Capability))
		}
	}
	return out
}

// validatePlanCompensation: plan-time refusal list (a node carries an undo its capability does not declare).
func validatePlanCompensation(nodes []Node, get func(string) *CapabilitySpec, strict bool) []string {
	var errs []string
	for _, n := range nodes {
		if n.Undo == "" {
			continue
		}
		cap := get(n.Capability)
		registered := ""
		if cap != nil {
			registered = declaredUndo(cap)
		}
		switch {
		case registered != n.Undo:
			rep := "None"
			if registered != "" {
				rep = pyq(registered)
			}
			errs = append(errs, fmt.Sprintf("node %s (%s): undo '%s' is not the registered undo (%s) of this capability", n.ID, n.Capability, n.Undo, rep))
		case strict && get(n.Undo) == nil:
			errs = append(errs, fmt.Sprintf("node %s (%s): undo capability '%s' is not registered (refused after 2027-01-01 / %s=1)", n.ID, n.Capability, n.Undo, EnvStrictTargets))
		case n.Undo == n.Capability:
			errs = append(errs, fmt.Sprintf("node %s (%s): undo is the forward capability itself", n.ID, n.Capability))
		}
	}
	return errs
}

func strictUndoTargets() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(EnvStrictTargets))) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	}
	return false // the sunset date (2027-01-01) has not passed in this build's lifetime; override with the env flag
}

// undoInputs: the EXACT inputs a compensating dispatch carries, derived from the logged forward result.
func undoInputs(node *Node, missionID string, forwardResult any) map[string]any {
	return map[string]any{"undo_of": node.Capability, "result": deepCopyJSON(forwardResult),
		"_node": node.ID + ":undo", "_mission": missionID}
}

func deepCopyJSON(v any) any {
	switch x := v.(type) {
	case map[string]any:
		o := make(map[string]any, len(x))
		for k, e := range x {
			o[k] = deepCopyJSON(e)
		}
		return o
	case []any:
		o := make([]any, len(x))
		for i, e := range x {
			o[i] = deepCopyJSON(e)
		}
		return o
	}
	return v
}

// receiptFor: the compensation's OWN receipt - what was invoked, under which key, and a digest of what came back.
func receiptFor(nodeID, undo, key string, result any) map[string]any {
	sum := sha256.Sum256([]byte(pyJSON(result)))
	return map[string]any{"node_id": nodeID, "undo": undo, "idempotency_key": key, "result_sha256": hex.EncodeToString(sum[:])}
}

// CompStatus is one node's folded compensation state.
type CompStatus struct {
	Status       string // requested|approval_required|authorized|executed_unverified|compensated|failed|acknowledged|skipped
	Undo         string
	Reason       string
	Attempts     int
	Attempt      int
	Approval     map[string]any
	Method       string
	Receipt      any
	Actor        string
	AckReason    string
	InputsSHA256 string
	seen         map[string]bool
}

func payloadInt(p map[string]any, k string) int {
	switch x := p[k].(type) {
	case float64:
		return int(x)
	case int:
		return x
	case int64:
		return int(x)
	case json.Number:
		n, _ := x.Int64()
		return int(n)
	}
	return 0
}

func payloadStr(p map[string]any, k string) string { s, _ := p[k].(string); return s }

// FoldCompensation: per-node compensation status folded from the durable log. `attempts` counts
// CompensationRequested; a duplicated record (same attempt number, or same event id for a legacy record carrying
// none) does not bump it.
func FoldCompensation(events []*Event) map[string]*CompStatus {
	out := map[string]*CompStatus{}
	for _, e := range events {
		if !compensationEvents[e.Type] {
			continue
		}
		p := e.Payload
		nid := payloadStr(p, "node_id")
		if nid == "" {
			continue
		}
		cur := out[nid]
		if cur == nil {
			cur = &CompStatus{Status: "requested", Undo: payloadStr(p, "undo"), seen: map[string]bool{}}
			out[nid] = cur
		}
		switch e.Type {
		case EvCompRequested:
			att := payloadInt(p, "attempt")
			dk := ""
			if att != 0 {
				dk = "a:" + strconv.Itoa(att)
			} else if e.ID != "" {
				dk = "e:" + e.ID
			}
			if dk != "" && cur.seen[dk] {
				continue
			}
			if dk != "" {
				cur.seen[dk] = true
			}
			cur.Status, cur.Undo, cur.Reason, cur.Approval, cur.Method = "requested", payloadStr(p, "undo"), "", nil, ""
			if att != 0 {
				cur.Attempt = att
			} else {
				cur.Attempt = cur.Attempts + 1
			}
			cur.Attempts++
		case EvCompApprovalRequired:
			cur.Status, cur.Reason, cur.InputsSHA256 = "approval_required", "awaiting_compensation_approval", payloadStr(p, "inputs_sha256")
		case EvCompApproved:
			if payloadInt(p, "attempt") == cur.Attempt {
				cur.Approval = p
			}
		case EvCompAuthorized:
			cur.Status = "authorized"
		case EvCompExecuted:
			cur.Status, cur.Receipt = "executed_unverified", p["receipt"]
		case EvCompVerified:
			cur.Status, cur.Method, cur.Actor = "compensated", payloadStr(p, "method"), payloadStr(p, "actor")
		case EvCompFailed:
			cur.Status, cur.Reason = "failed", payloadStr(p, "reason")
		case EvCompAcknowledged:
			cur.Status, cur.Actor, cur.AckReason = "acknowledged", payloadStr(p, "actor"), payloadStr(p, "reason")
		case EvCompSkipped:
			cur.Status, cur.Reason = "skipped", payloadStr(p, "reason")
		}
	}
	return out
}

// CompSummary is the mission-outcome view of a fold; `Compensated` holds ONLY verified compensations.
type CompSummary struct {
	Compensated           []string
	CompensatedUnverified []string
	CompensationFailed    map[string]string
	CompensationAck       []string
	CompensationSkipped   map[string]string
	ReadbackOnly          bool
}

func SummarizeCompensation(f map[string]*CompStatus) CompSummary {
	by := func(st string) []string {
		out := []string{}
		for n, v := range f {
			if v.Status == st {
				out = append(out, n)
			}
		}
		sort.Strings(out)
		return out
	}
	s := CompSummary{Compensated: by("compensated"), CompensatedUnverified: by("executed_unverified"),
		CompensationAck: by("acknowledged"), CompensationFailed: map[string]string{}, CompensationSkipped: map[string]string{}, ReadbackOnly: true}
	for n, v := range f {
		switch v.Status {
		case "failed", "approval_required":
			s.CompensationFailed[n] = v.Reason
		case "skipped":
			s.CompensationSkipped[n] = v.Reason
		case "compensated":
			if v.Method != "readback" {
				s.ReadbackOnly = false
			}
		}
	}
	return s
}
