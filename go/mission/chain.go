package mission

// Tamper-evident event log: a per-mission hash chain. Go port of the chain in agentic_os/mission/store.py.
//
//	hash = sha256( prev + "|" + canonical ), canonical = json.dumps({seq, ts, type, mission_id, payload},
//	               sort_keys=True, separators=(",", ":"), ensure_ascii=False)   (UTF-8)
//
// prev is the hash of the PREVIOUS event OF THE SAME MISSION ("" for its first). Byte-identical to Python: the shared
// vectors in conformance/vectors/chain.json are asserted by conformance_vectors_test.go. The canonicalisation is NOT
// the one the approval digest uses (that is ensure_ascii=True): never reuse canonicalJSON here.
//
// What it does not give: no signing, and tail truncation is invisible to the log alone — anchor Head() somewhere the
// log's writer cannot change. Only the JSONL and in-memory stores chain (there are no other Go backends).

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
)

// ChainReport is the outcome of VerifyChain.
type ChainReport struct {
	OK          bool   `json:"ok"`
	Checked     int    `json:"checked"`   // hashed events verified
	Unchained   int    `json:"unchained"` // legacy events with no hash (not covered)
	FirstBadSeq int    `json:"first_bad_seq"`
	Reason      string `json:"reason"`
}

// chainPayload is the payload as it must be hashed: the raw JSON text read from the log when the event was loaded (so
// `1.0` stays `1.0` and big integers stay exact), else the in-memory payload normalised through encoding/json (what
// would have been written).
func (e *Event) chainPayload() any {
	if len(e.raw) > 0 {
		d := json.NewDecoder(bytes.NewReader(e.raw))
		d.UseNumber()
		var v any
		if d.Decode(&v) == nil {
			return v
		}
	}
	if e.Payload == nil {
		return map[string]any(nil)
	}
	if _, err := json.Marshal(e.Payload); err != nil {
		// NaN/Inf are legal in the log (Python bare tokens) and are written by pyFloat; a payload that cannot be
		// serialised at all (a cycle) is hashed as a fixed marker rather than recursed into.
		if _, serr := sanitizeNonFinite(e.Payload); serr != nil {
			return map[string]any{"_unserialisable_payload": true}
		}
		return e.Payload
	}
	return normalizeNumbers(e.Payload)
}

func chainCanonical(seq int, ts float64, typ, missionID string, payload any) []byte {
	var sb strings.Builder
	writePyJSONOpts(&sb, map[string]any{"seq": seq, "ts": ts, "type": typ, "mission_id": missionID, "payload": payload},
		",", ":", false)
	return []byte(sb.String())
}

// EntryHash is sha256(prev + "|" + canonical(seq, ts, type, mission_id, payload)) as lowercase hex.
func EntryHash(prev string, seq int, ts float64, typ, missionID string, payload any) string {
	h := sha256.New()
	h.Write([]byte(prev))
	h.Write([]byte("|"))
	h.Write(chainCanonical(seq, ts, typ, missionID, payload))
	return hex.EncodeToString(h.Sum(nil))
}

// VerifyChain checks each mission's chain: every hashed event must hash correctly and link to its predecessor, so an
// edited, deleted or reordered event is detected. Events with no hash (written before chaining) are counted as
// unchained and not covered.
func VerifyChain(events []*Event) ChainReport {
	heads := map[string]string{}
	var checked, unchained int
	for _, ev := range events {
		if ev.Hash == "" {
			unchained++
			continue
		}
		if ev.Prev != heads[ev.MissionID] {
			return ChainReport{false, checked, unchained, ev.Seq, "an event is missing, reordered or replaced"}
		}
		if EntryHash(ev.Prev, ev.Seq, ev.Ts, ev.Type, ev.MissionID, ev.chainPayload()) != ev.Hash {
			return ChainReport{false, checked, unchained, ev.Seq, "an event was modified"}
		}
		heads[ev.MissionID] = ev.Hash
		checked++
	}
	return ChainReport{true, checked, unchained, 0, ""}
}

// Head is the chain head (hash of the mission's last event). Anchor it outside the log writer's reach.
func (s *EventStore) Head(missionID string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.heads[missionID]
}

// Verify checks every mission's chain in this store.
func (s *EventStore) Verify() ChainReport {
	s.mu.Lock()
	evs := append([]*Event{}, s.events...)
	s.mu.Unlock()
	return VerifyChain(evs)
}
