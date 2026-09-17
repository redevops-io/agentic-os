package mission

// Normalized runtime events — the public event vocabulary + a local, replayable event store.
//
// Go port of agentic_os/mission/events.py (the executable specification). Every supported agent
// surface normalizes into the same typed, replayable event history: one content-addressed
// RuntimeEvent envelope (deterministic id → replayable), the versioned per-record SCHEMAS +
// Validate, and an append-only EventLedger with causal edges + NDJSON serialization.
//
// The content-addressed event_id and the NDJSON encoding are BIT-IDENTICAL to the Python (and
// Kotlin) implementations: the id key embeds Python's list repr for the sorted artifact handles and
// json.dumps(payload, sort_keys=True); NDJSON is json.dumps(dict, sort_keys=True) — both with
// Python's default ", " / ": " separators. pyJSON + pyListRepr below reproduce that exactly, so an
// event recorded on one runtime is verifiable on another. Contract version SchemaVersion below.

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// EventType is the public event vocabulary.
type EventType string

const (
	EvDereference          EventType = "dereference"
	EvCapabilityInvocation EventType = "capability_invocation"
	EvToolProposal         EventType = "tool_proposal"
	EvToolResult           EventType = "tool_result"
	EvVerification         EventType = "verification"
	EvMerge                EventType = "merge"
	EvPromotion            EventType = "promotion"
	EvPolicyDecision       EventType = "policy_decision"
	EvHumanReview          EventType = "human_review"
	// EvEvidenceChange — a world/evidence delta on the governance timeline (v0.2.x Slice 4), so
	// Governance can correlate an evidence-change series against an action series.
	EvEvidenceChange EventType = "evidence_change"
)

// ResultStatus is the outcome of the event's action.
type ResultStatus string

const (
	RsProposed  ResultStatus = "proposed"
	RsAttempted ResultStatus = "attempted"
	RsCompleted ResultStatus = "completed"
	RsFailed    ResultStatus = "failed"
	RsObserved  ResultStatus = "observed"
	RsDenied    ResultStatus = "denied"
)

// Severity ranks an event's importance (informational < low < medium < high < critical).
type Severity string

const (
	SevInformational Severity = "informational"
	SevLow           Severity = "low"
	SevMedium        Severity = "medium"
	SevHigh          Severity = "high"
	SevCritical      Severity = "critical"
)

var severityOrder = []Severity{SevInformational, SevLow, SevMedium, SevHigh, SevCritical}

// Rank returns the ordinal of a severity (-1 if unknown).
func (s Severity) Rank() int {
	for i, v := range severityOrder {
		if v == s {
			return i
		}
	}
	return -1
}

// SchemaVersion is the semantic version of the public runtime-event contract (envelope + NDJSON).
const SchemaVersion = "runtime-event/v10"

// ContractVersion is an alias kept for parity with the Python module surface.
const ContractVersion = SchemaVersion

// SCHEMAS enumerates the required fields per record kind (the v9 amendment's versioned schemas).
var SCHEMAS = map[string][]string{
	"runtime-event/v10":     {"event_id", "event_type", "schema_version", "timestamp", "actor", "result_status"},
	"dereference-event/v1":  {"event_id", "artifact_handles", "visibility"},
	"capability-event/v1":   {"event_id", "capability_id"},
	"verification-event/v1": {"event_id", "result_status"},
	"policy-decision/v1":    {"event_id", "policy_context"},
	"session-summary/v1":    {"session_id", "event_count"},
}

// RuntimeEvent is the one normalized envelope every subsystem emits — Context/Mission/Discovery
// Runtimes, Sidekick and external-agent adapters share this single forensic timeline.
type RuntimeEvent struct {
	EventType       EventType      `json:"event_type"`
	Actor           string         `json:"actor"`
	Timestamp       int            `json:"timestamp"` // caller-supplied logical time (deterministic/replayable)
	SchemaVersion   string         `json:"schema_version"`
	MissionID       string         `json:"mission_id"`
	SessionID       string         `json:"session_id"`
	ParentEventID   string         `json:"parent_event_id"`
	CapabilityID    string         `json:"capability_id"`
	ArtifactHandles []string       `json:"artifact_handles"`
	SourceRuntime   string         `json:"source_runtime"`
	SourceAgent     string         `json:"source_agent"`
	Visibility      string         `json:"visibility"`
	PolicyContext   string         `json:"policy_context"`
	ResultStatus    ResultStatus   `json:"result_status"`
	EvidenceRefs    []string       `json:"evidence_refs"`
	Payload         map[string]any `json:"payload"`
	EventID         string         `json:"event_id"`
}

// finalize applies the envelope defaults and stamps the content-addressed id (mirrors
// RuntimeEvent.__post_init__). Called by every constructor; keep it the single id authority.
func (e *RuntimeEvent) finalize() {
	if e.SchemaVersion == "" {
		e.SchemaVersion = SchemaVersion
	}
	if e.ResultStatus == "" {
		e.ResultStatus = RsObserved
	}
	if e.Visibility == "" {
		e.Visibility = "private"
	}
	if e.ArtifactHandles == nil {
		e.ArtifactHandles = []string{}
	}
	if e.EvidenceRefs == nil {
		e.EvidenceRefs = []string{}
	}
	if e.Payload == nil {
		e.Payload = map[string]any{}
	}
	if e.EventID == "" { // content-addressed id → deterministic, replayable
		handles := append([]string{}, e.ArtifactHandles...)
		sort.Strings(handles)
		key := strings.Join([]string{
			string(e.EventType), e.Actor, strconv.Itoa(e.Timestamp), e.MissionID,
			e.ParentEventID, pyListRepr(handles), pyJSON(e.Payload),
		}, "|")
		sum := sha256.Sum256([]byte(key))
		e.EventID = "ev-" + fmt.Sprintf("%x", sum)[:16]
	}
}

// NewRuntimeEvent builds an event with defaults applied and its content-addressed id stamped.
func NewRuntimeEvent(e RuntimeEvent) RuntimeEvent {
	e.finalize()
	return e
}

// ndjsonMap projects the event to the ordered field set json.dumps serializes (enum values as
// their strings), so ToNDJSON reproduces the Python bytes exactly.
func (e RuntimeEvent) ndjsonMap() map[string]any {
	return map[string]any{
		"event_type":       string(e.EventType),
		"actor":            e.Actor,
		"timestamp":        e.Timestamp,
		"schema_version":   e.SchemaVersion,
		"mission_id":       e.MissionID,
		"session_id":       e.SessionID,
		"parent_event_id":  e.ParentEventID,
		"capability_id":    e.CapabilityID,
		"artifact_handles": stringsToAny(e.ArtifactHandles),
		"source_runtime":   e.SourceRuntime,
		"source_agent":     e.SourceAgent,
		"visibility":       e.Visibility,
		"policy_context":   e.PolicyContext,
		"result_status":    string(e.ResultStatus),
		"evidence_refs":    stringsToAny(e.EvidenceRefs),
		"payload":          e.Payload,
		"event_id":         e.EventID,
	}
}

// ToNDJSON serializes the event as json.dumps(asdict, sort_keys=True) — Python's default separators.
func (e RuntimeEvent) ToNDJSON() string { return pyJSON(e.ndjsonMap()) }

// RuntimeEventFromNDJSON reconstructs an event from one NDJSON line. The event_id is carried
// verbatim (never recomputed), so a round-trip is byte-stable.
func RuntimeEventFromNDJSON(line string) (RuntimeEvent, error) {
	var raw map[string]any
	dec := json.NewDecoder(strings.NewReader(line))
	dec.UseNumber()
	if err := dec.Decode(&raw); err != nil {
		return RuntimeEvent{}, err
	}
	ev := RuntimeEvent{
		EventType:       EventType(asString(raw["event_type"])),
		Actor:           asString(raw["actor"]),
		Timestamp:       asInt(raw["timestamp"]),
		SchemaVersion:   asString(raw["schema_version"]),
		MissionID:       asString(raw["mission_id"]),
		SessionID:       asString(raw["session_id"]),
		ParentEventID:   asString(raw["parent_event_id"]),
		CapabilityID:    asString(raw["capability_id"]),
		ArtifactHandles: anyToStrings(raw["artifact_handles"]),
		SourceRuntime:   asString(raw["source_runtime"]),
		SourceAgent:     asString(raw["source_agent"]),
		Visibility:      asString(raw["visibility"]),
		PolicyContext:   asString(raw["policy_context"]),
		ResultStatus:    ResultStatus(asString(raw["result_status"])),
		EvidenceRefs:    anyToStrings(raw["evidence_refs"]),
		Payload:         asMap(raw["payload"]),
		EventID:         asString(raw["event_id"]), // preserved, not recomputed
	}
	if ev.Payload == nil {
		ev.Payload = map[string]any{}
	}
	return ev, nil
}

// Validate returns the missing required fields per the envelope + the type's specialized schema
// (empty slice = valid). Mirrors RuntimeEvent.validate.
func (e RuntimeEvent) Validate() []string {
	present := e.ndjsonMap()
	// event_id is required and always populated after finalize; a zero envelope still reports gaps.
	miss := func(fields []string) []string {
		var out []string
		for _, f := range fields {
			if isEmptyField(present[f]) {
				out = append(out, f)
			}
		}
		return out
	}
	missing := miss(SCHEMAS["runtime-event/v10"])
	specSchema := map[EventType]string{
		EvDereference:          "dereference-event/v1",
		EvCapabilityInvocation: "capability-event/v1",
		EvVerification:         "verification-event/v1",
		EvPolicyDecision:       "policy-decision/v1",
	}[e.EventType]
	if specSchema != "" {
		missing = append(missing, miss(SCHEMAS[specSchema])...)
	}
	return missing
}

// ─── specialization constructors (thin — the envelope is the contract) ───────

// DereferenceEvent records an artifact dereference (which handles, at what visibility).
func DereferenceEvent(actor string, timestamp int, artifactHandles []string, visibility string, base RuntimeEvent) RuntimeEvent {
	base.EventType, base.Actor, base.Timestamp = EvDereference, actor, timestamp
	base.ArtifactHandles = artifactHandles
	if visibility != "" {
		base.Visibility = visibility
	}
	return NewRuntimeEvent(base)
}

// CapabilityEvent records a capability invocation (defaults to ATTEMPTED).
func CapabilityEvent(actor string, timestamp int, capabilityID string, base RuntimeEvent) RuntimeEvent {
	base.EventType, base.Actor, base.Timestamp = EvCapabilityInvocation, actor, timestamp
	base.CapabilityID = capabilityID
	if base.ResultStatus == "" {
		base.ResultStatus = RsAttempted
	}
	return NewRuntimeEvent(base)
}

// ToolResultEvent records a tool result (defaults to COMPLETED).
func ToolResultEvent(actor string, timestamp int, capabilityID string, base RuntimeEvent) RuntimeEvent {
	base.EventType, base.Actor, base.Timestamp = EvToolResult, actor, timestamp
	base.CapabilityID = capabilityID
	if base.ResultStatus == "" {
		base.ResultStatus = RsCompleted
	}
	return NewRuntimeEvent(base)
}

// PolicyDecisionEvent records a governance decision on the timeline.
func PolicyDecisionEvent(actor string, timestamp int, policyContext string, resultStatus ResultStatus, base RuntimeEvent) RuntimeEvent {
	base.EventType, base.Actor, base.Timestamp = EvPolicyDecision, actor, timestamp
	base.PolicyContext, base.ResultStatus = policyContext, resultStatus
	return NewRuntimeEvent(base)
}

// EvidenceChangeEvent records a world/evidence delta (v0.2.x Slice 4): ref is the logical artifact
// id and changeType is created|updated|deleted; both land in the payload and evidence_refs so a
// trajectory rule can correlate the evidence series with a following action series.
func EvidenceChangeEvent(actor string, timestamp int, ref, changeType string, base RuntimeEvent) RuntimeEvent {
	base.EventType, base.Actor, base.Timestamp = EvEvidenceChange, actor, timestamp
	payload := map[string]any{}
	for k, v := range base.Payload {
		payload[k] = v
	}
	payload["ref"], payload["change_type"] = ref, changeType
	base.Payload = payload
	base.EvidenceRefs = []string{ref}
	base.ResultStatus = RsObserved
	return NewRuntimeEvent(base)
}

// ─── EventLedger — append-only, physically-ordered history with causal edges ──

// EventLedger never mutates or erases: a model rewind, an abandoned branch, and a superseded
// proposal are all retained. This is the minimal local store; enterprise ingestion consumes the
// same NDJSON. Mirrors events.py::EventLedger.
type EventLedger struct {
	events []RuntimeEvent
	byID   map[string]RuntimeEvent
}

// NewEventLedger builds an empty ledger.
func NewEventLedger() *EventLedger {
	return &EventLedger{byID: map[string]RuntimeEvent{}}
}

// Append records an event in physical order (never rewritten).
func (l *EventLedger) Append(e RuntimeEvent) RuntimeEvent {
	l.events = append(l.events, e)
	l.byID[e.EventID] = e
	return e
}

// Events returns the ledger in physical order.
func (l *EventLedger) Events() []RuntimeEvent { return append([]RuntimeEvent{}, l.events...) }

// Get returns the event with the given id (ok=false if absent).
func (l *EventLedger) Get(eventID string) (RuntimeEvent, bool) {
	e, ok := l.byID[eventID]
	return e, ok
}

// Children returns the events whose parent is eventID, in physical order.
func (l *EventLedger) Children(eventID string) []RuntimeEvent {
	var out []RuntimeEvent
	for _, e := range l.events {
		if e.ParentEventID == eventID {
			out = append(out, e)
		}
	}
	return out
}

// BySession returns the events in one session, in physical order.
func (l *EventLedger) BySession(sessionID string) []RuntimeEvent {
	var out []RuntimeEvent
	for _, e := range l.events {
		if e.SessionID == sessionID {
			out = append(out, e)
		}
	}
	return out
}

// ToNDJSON serializes the whole ledger, one event per line, in physical order.
func (l *EventLedger) ToNDJSON() string {
	lines := make([]string, len(l.events))
	for i, e := range l.events {
		lines[i] = e.ToNDJSON()
	}
	return strings.Join(lines, "\n")
}

// EventLedgerFromNDJSON replays a ledger from NDJSON text (blank lines skipped).
func EventLedgerFromNDJSON(text string) (*EventLedger, error) {
	led := NewEventLedger()
	for _, line := range strings.Split(text, "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		ev, err := RuntimeEventFromNDJSON(line)
		if err != nil {
			return nil, err
		}
		led.Append(ev)
	}
	return led, nil
}

// SessionSummary is the session-summary/v1 record: count, distinct types (sorted), failures.
func (l *EventLedger) SessionSummary(sessionID string) map[string]any {
	evs := l.BySession(sessionID)
	typeSet := map[string]bool{}
	failed := 0
	for _, e := range evs {
		typeSet[string(e.EventType)] = true
		if e.ResultStatus == RsFailed {
			failed++
		}
	}
	types := make([]string, 0, len(typeSet))
	for t := range typeSet {
		types = append(types, t)
	}
	sort.Strings(types)
	return map[string]any{
		"schema_version": "session-summary/v1",
		"session_id":     sessionID,
		"event_count":    len(evs),
		"types":          stringsToAny(types),
		"failed":         failed,
	}
}

// ─── Python-compatible encoders (bit-identity with json.dumps / list repr) ───

// pyJSON reproduces Python's json.dumps(v, sort_keys=True) with its DEFAULT separators (", " and
// ": ") and ensure_ascii escaping — the exact bytes the event_id key and NDJSON depend on.
func pyJSON(v any) string {
	var sb strings.Builder
	writePyJSON(&sb, v)
	return sb.String()
}

func writePyJSON(sb *strings.Builder, v any) {
	switch x := v.(type) {
	case nil:
		sb.WriteString("null")
	case bool:
		if x {
			sb.WriteString("true")
		} else {
			sb.WriteString("false")
		}
	case string:
		writePyString(sb, x)
	case int:
		sb.WriteString(strconv.Itoa(x))
	case int64:
		sb.WriteString(strconv.FormatInt(x, 10))
	case float64:
		sb.WriteString(pyFloat(x))
	case json.Number:
		sb.WriteString(string(x)) // preserve the parsed token (int or fractional) verbatim
	case []any:
		sb.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				sb.WriteString(", ")
			}
			writePyJSON(sb, e)
		}
		sb.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		sb.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				sb.WriteString(", ")
			}
			writePyString(sb, k)
			sb.WriteString(": ")
			writePyJSON(sb, x[k])
		}
		sb.WriteByte('}')
	default:
		// Fallback: route anything else through encoding/json (rare; payloads are plain JSON).
		b, _ := json.Marshal(x)
		sb.Write(b)
	}
}

// writePyString escapes a string as Python json.dumps (ensure_ascii=True): escape " \ and the C0
// controls with short escapes where Python does (\b \f \n \r \t), everything else non-ASCII as
// \uXXXX, printable ASCII verbatim.
func writePyString(sb *strings.Builder, s string) {
	sb.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			sb.WriteString(`\"`)
		case '\\':
			sb.WriteString(`\\`)
		case '\b':
			sb.WriteString(`\b`)
		case '\f':
			sb.WriteString(`\f`)
		case '\n':
			sb.WriteString(`\n`)
		case '\r':
			sb.WriteString(`\r`)
		case '\t':
			sb.WriteString(`\t`)
		default:
			if r < 0x20 || r > 0x7e {
				if r > 0xffff { // surrogate pair, as Python \uXXXX\uXXXX
					r -= 0x10000
					hi := 0xd800 + (r >> 10)
					lo := 0xdc00 + (r & 0x3ff)
					fmt.Fprintf(sb, `\u%04x\u%04x`, hi, lo)
				} else {
					fmt.Fprintf(sb, `\u%04x`, r)
				}
			} else {
				sb.WriteRune(r)
			}
		}
	}
	sb.WriteByte('"')
}

// pyFloat renders a float as Python's repr would for common values (shortest round-trip). Full
// parity with CPython's float repr across all inputs is out of scope; event payloads are plain data
// and rarely carry floats — integers, strings and nested maps dominate.
func pyFloat(f float64) string {
	return strconv.FormatFloat(f, 'g', -1, 64)
}

// pyListRepr reproduces Python's repr() of a list of strings: [] or ['a', 'b'] with single quotes,
// ", " separators, and backslash/quote escaping — the exact form embedded in the event_id key.
func pyListRepr(items []string) string {
	parts := make([]string, len(items))
	for i, s := range items {
		esc := strings.ReplaceAll(s, `\`, `\\`)
		esc = strings.ReplaceAll(esc, `'`, `\'`)
		parts[i] = "'" + esc + "'"
	}
	return "[" + strings.Join(parts, ", ") + "]"
}

// ─── small conversion helpers ────────────────────────────────────────────────

func stringsToAny(ss []string) []any {
	out := make([]any, len(ss))
	for i, s := range ss {
		out[i] = s
	}
	return out
}

func anyToStrings(v any) []string {
	arr, ok := v.([]any)
	if !ok {
		return []string{}
	}
	out := make([]string, 0, len(arr))
	for _, e := range arr {
		out = append(out, asString(e))
	}
	return out
}

func asString(v any) string {
	s, _ := v.(string)
	return s
}

func asMap(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func asInt(v any) int {
	switch n := v.(type) {
	case json.Number:
		i, _ := n.Int64()
		return int(i)
	case float64:
		return int(n)
	case int:
		return n
	}
	return 0
}

// isEmptyField mirrors Python's `not d.get(f)` truthiness for the Validate check: empty string,
// empty slice, empty map, nil, 0, and false all count as missing.
func isEmptyField(v any) bool {
	switch x := v.(type) {
	case nil:
		return true
	case string:
		return x == ""
	case []any:
		return len(x) == 0
	case map[string]any:
		return len(x) == 0
	case int:
		return x == 0
	case bool:
		return !x
	}
	return false
}
