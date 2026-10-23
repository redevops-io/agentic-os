# Go Mission Runtime parity review (read-only)
Go: /Users/arybach/agentic-os/go/mission (62 .go files). `go` is NOT installed -> `go test ./...` not run (UNVERIFIED).
Py ref: /Users/arybach/agentic-os/agentic_os/mission. Paths below abbreviated: G=go/mission, P=agentic_os/mission.

| # | Status | Go evidence | Python ref | Tests |
|---|--------|-------------|-----------|-------|
| B1 park digest/summary | ABSENT | G/runtime.go:545-571 `park` appends NodeParked with rules/risk/packet/human_task only: no inputs_sha256, no action_summary | P/runtime.py:1076-1096 _park; _action_summary 376; _node_inputs_digest 368 | none |
| B1 ApprovalGranted records digest | ABSENT | runtime.go:680 payload = {node_id, edit} only (no approver, capability, inputs_sha256) | runtime.py:1284-1289 | none |
| B1 dispatch recompute + ApprovalInvalidated on mismatch | ABSENT | no digest compare in run loop. Only ApprovalInvalidated emitted is policy_tightened (runtime.go:880); folded at 1183, compensation_runner.go:103, fork.go:96. Approved nodes run unconditionally | runtime.py:739-748, _approved_digest 392 | none |
| B1 legacy grandfathering | N/A-moot (nothing to grandfather; Go never writes digest). Reading Python logs with digests: Go ignores them |  | | |
| B1 compensation-side binding (extra) | PRESENT | compensation_runner.go:1009-1013 / executor.go:183 bind undo approval to InputsHash + exp (ApprovalTTLSeconds=3600) | compensation.py | compensation_parity_test.go |
| B2 HMAC token bound to op/cap/mission/node/inputs/exp/nonce | PRESENT | G/approval.go:113-139 mint, 143-199 verify (fail-closed binding, hmac.Equal, exp, marker dict, replay consume); HTTPOperatorClient mints 300s tokens (operators.go:80); Operator verifies (operator_sdk.go:200). Gap: no file-secret fallback (documented, approval.go:92) | P/approval.py:103-151 | approval_parity_test.go:102 (byte-identical mint), 126 (verify), 149 (single use), 254 (py-minted int inputs), 288, 332 + testdata/approval_tokens.json |
| B2 single-use replay | PRESENT (in-memory) | approval.go:202-235 bounded, fail-closed when full | approval.py:181-205 | approval_parity_test.go:149,351 |
| B3 SqliteReplayGuard / env vars | ABSENT | ReplayGuard is per-process only (comment approval.go:202, operator_sdk.go:77). No AGENTIC_OS_REPLAY_DB / REQUIRE_DURABLE_REPLAY (grep: only APPROVAL_SECRET and compensation envs exist). Operator{ReplayGuard} field is injectable, so a durable impl needs only Consume(nonce,exp,now) - but the field type is concrete *ReplayGuard, not an interface | approval.py:208-294 | none |
| B4 per-mission hash chain | ABSENT | G/store.go:14-21 Event has only Type/MissionID/Payload/Seq/ID/Ts - no Prev/Hash; Append (store.go:68) no hashing; no VerifyChain/Head. Go EventStore also is ONE global seq (same as py). | P/store.py:59-106, head 223, receipt.py | none |
| B4 receipt ledger_head/ledger_chain | ABSENT | no mission_receipt in Go (grep receipt: only compensation receipts) | receipt.py:18-57 | none |
| B5 approval TTL | ABSENT (mission gates) | no AGENTIC_OS_APPROVAL_TTL_S, no ApprovalExpired, Approve (runtime.go:645) has no age check, no error return. Unrelated 3600s constant is compensation-approval exp only | runtime.py:99-106, 1265-1275 | none |
| B6 assurance sensors | ABSENT | no assurance*.go; governance.go only lists ApprovalInvalidated as an intervention kind | assurance.py:37-98 | none |
| B7 idempotency key to handler | PARTIAL | key sent in body + Idempotency-Key header (operators.go:72,87); server-side dedupe keyed on (cap, canonical inputs, key) caching only successes (operator_sdk.go:108-160); BUT handler is `fn(inputs)` only (operator_sdk.go:146) - no ContextVar/ctx equivalent of CURRENT_IDEMPOTENCY_KEY, so a handler cannot forward the key to a downstream provider | operator_sdk.py:133,208-212; gateway external/operator.py:77 | approval_parity_test.go:222,398 (dedupe) |
| B8 ambiguous outcome | ABSENT (unsafe default) | executor.go:115 returns err for any client error incl. HTTP timeout/5xx; runtime.go:419-427 failNode -> NodeFailed + `rt.saga` (compensation) + fail, i.e. treated as clean failure. No "unknown/in-doubt" state, no read-back before compensation on the mission path. Mitigation: compensation is explicit-opt-in only (compensation_policy.go: default failure policy preserve-only; AGENTIC_OS_DEFAULT_FAILURE_POLICY) and runSaga only considers nodes recorded DONE (runtime.go:~700), and node that errored is NOT done, so its own possibly-landed effect is neither verified nor compensated; the retry reuses the same idempotency key (compiler.go:110) which is the safe part. Same gap as Python (issue #526) | n/a (open #526) | none |
| B9 backends | JSONL + in-memory only | store.go:27 NewEventStore(path) JSONL append (no fsync option, no tail-torn-line repair seen), path=="" is memory. events.go has a separate content-addressed RuntimeEvent ledger (NDJSON, events.go:100-361) - dedupes by sha256 id, NOT a chain. No DuckDB, no Postgres | P/store.py EventStore/MemoryEventStore; P/event_backends.py:55 DuckDB, :110 Postgres (neither chain in py: no hash fields) | store_test.go, events_test.go |

Chaining per backend: Go JSONL = no; Go memory = no; Go RuntimeEvent ledger = no (content id only). Python: only JSONL EventStore + MemoryEventStore chain; DuckDB/Postgres python backends do not either.

## B4 byte-identity analysis (hypothetical Go chain vs Python)
Python (store.py:69-77): json.dumps({seq,ts,type,mission_id,payload}, sort_keys=True, separators=(",",":"), **ensure_ascii=False**, default=str).encode("utf-8"); hash = sha256(prev_ascii + b"|" + that).
Go today: nothing implements this. Reusable helper `canonicalJSON` (approval.go:67 -> writePyJSONSep, events.go:405-536) matches json.dumps with **ensure_ascii=True** (events.go:474 escapes non-ASCII as \uXXXX incl. surrogate pairs). Reusing it as-is WOULD PRODUCE DIFFERENT HASHES for any non-ASCII payload (names, emails, goals). Need a variant with raw UTF-8 output.
Cross-language risk list:
1. ensure_ascii: chain=False (raw UTF-8), approval hash=True (escaped). Two different modes must coexist; do not share a flag by accident. Python also escapes only " \ and C0 controls in this mode; DEL (0x7f) raw; U+2028/2029 raw. Invalid/lone surrogates: Python allows in str, encode("utf-8") would raise; Go would substitute U+FFFD.
2. Number types: Python keeps int vs float distinct (5 vs 5.0, 10.0 stays "10.0"). Go decoding JSON into map[string]any gives float64 for all numbers => 5 would serialise "5.0" (or "5") and diverge, and ints > 2^53 lose precision. Must decode with UseNumber and re-emit verbatim (approval.go normalizeNumbers does this for inputs; the event load path (decodeEventLine) must be verified to do the same before any chain verification of Python-written logs). Go Event.Ts is float64; seq int.
3. Float formatting: Python repr (shortest round trip, exponent when >=1e16 or <1e-4, "1e+20", "1e-07", "-0.0"). Go pyFloat (events.go:516) implements this; testdata/approval_tokens.json covers 1e+20, 1e-07, -0.0. NaN/Infinity emitted as bare tokens in Python (allow_nan default); Go pyFloat handles. ts values: Python time.time() has ~microsecond float; hash must use the ts as persisted, parse-and-reformat must round-trip (repr(float(x)) == x). A ts persisted by Go via encoding/json (strconv 'g'-ish, e.g. 1.7e+09 forms) can re-format differently than Python repr: always hash from the parsed value with pyFloat, never the raw text.
4. Key ordering: Python sort_keys sorts by Unicode code point of str; Go sort.Strings sorts by UTF-8 byte, which is the same order for BMP+astral EXCEPT it differs from Python for astral vs high-BMP (U+E000-U+FFFF vs U+10000+): Python (code point) puts U+FFFF < U+10000, UTF-8 byte order also does -> equal. Equal in practice; but UTF-16 sorters (JS) differ. Verify writePyJSONSep sorts keys at EVERY nesting level, incl. keys of non-string type (Python coerces int keys to str before sorting; Go map[string] cannot hit this).
5. default=str: Python stringifies non-JSON values (datetime, Enum, set, dataclass) via str(); Go payloads are jsonable() wrapped (store.go comment) - enum/struct rendering may differ from str(), so cross-language chains over such payloads diverge. Python-only payload values cannot be re-hashed by Go except from the persisted JSON.
6. Empty vs nil: Go nil slice/map serialise null vs Python [] / {}; omitted fields (json `omitempty`, Event.ID) must not enter the hashed envelope. Hashed envelope is exactly 5 keys.
7. prev for first event is "" (empty), joined as "|" prefix: hash input starts with b"|".
8. Per-mission chain over a global seq store: seq gaps between a mission's events are normal; Go must chain per mission_id and tolerate legacy unchained (hash=="") lines (counted unchained, not failed). Go's decodeEventLine must preserve unknown fields prev/hash on load or a Go rewrite/compaction (compaction.go) would silently drop them and break the chain.
9. Concurrency: Python holds a lock across read-prev/compute/append; Go Append already serialises under s.mu, so chain-on-append is straightforward.

## Conformance fixtures recommendation
B2 already has shared vectors (go/mission/testdata/approval_tokens.json, asserted at approval_parity_test.go:102-149); keep, and add: (a) a non-ASCII + nested + 1e+20 case already present, (b) a Python-minted token with fixed nonce+now (token below has random nonce, so for a stable fixture regenerate via approval.py with a patched _secrets.token_hex, as testdata does).
B4 needs NEW vectors (none exist). Generated by /private/tmp/claude-501/-Users-arybach-agentic-os/30e85532-95c8-480d-aadc-1f976c943ab3/scratchpad/parity/gen.py (PYTHONPATH=/Users/arybach/agentic-os venv-ent python); raw output saved in parity/vectors.out. Chain vector (mission m1, ts=1700000000.123456+seq, ensure_ascii=False hashing; payload 1 deliberately contains "café ☃ \U0001F600", int 5, float 10.0, 1e20, nested unsorted keys, null/true):
```
{
 "chain": [
  {
   "seq": 1,
   "ts": 1700000001.123456,
   "type": "MissionCreated",
   "mission_id": "m1",
   "payload": {
    "goal": "caf\u00e9 \u2603 \ud83d\ude00",
    "n": 5,
    "f": 10.0,
    "big": 1e+20,
    "z": {
     "b": 1,
     "a": [
      1,
      2.5,
      null,
      true
     ]
    }
   },
   "prev": "",
   "hash": "ab31ec45850dc3f4157f27773a56946aa858e0916ded10a950e9c33c12903183"
  },
  {
   "seq": 2,
   "ts": 1700000002.123456,
   "type": "NodeParked",
   "mission_id": "m1",
   "payload": {
    "node_id": "n1",
    "amount": 12.5,
    "small": 1e-07
   },
   "prev": "ab31ec45850dc3f4157f27773a56946aa858e0916ded10a950e9c33c12903183",
   "hash": "d1dd0137f9da548e19295f020a4077eae7a82f6d92df824c44aaab2a3fa5df52"
  },
  {
   "seq": 3,
   "ts": 1700000003.123456,
   "type": "ApprovalGranted",
   "mission_id": "m1",
   "payload": {
    "node_id": "n1",
    "inputs_sha256": "ab"
   },
   "prev": "d1dd0137f9da548e19295f020a4077eae7a82f6d92df824c44aaab2a3fa5df52",
   "hash": "b0219c320f75cfa1703e23a25ec3c875f616c3eebdccc04ebae233a548c9624e"
  }
 ],
 "head": "b0219c320f75cfa1703e23a25ec3c875f616c3eebdccc04ebae233a548c9624e"
}
inputs_sha256 62751459e9aad0bd4ea583f1c4126b39cb68e6691a798d94d5134cf4fcba7ffb
token eyJjYXBhYmlsaXR5IjoiY2FwIiwiZXhwIjoxNzAwMDAwMzAwLjAsImlucHV0c19zaGEyNTYiOiI2Mjc1MTQ1OWU5YWFkMGJkNGVhNTgzZjFjNDEyNmIzOWNiNjhlNjY5MWE3OThkOTRkNTEzNGNmNGZjYmE3ZmZiIiwibWFya2VyIjp7ImFwcHJvdmVkIjp0cnVlfSwibWlzc2lvbl9pZCI6Im0xIiwibm9kZV9pZCI6Im4xIiwibm9uY2UiOiIxZTVlMTU3Yjc1NTdlMTdiZDA2ZmNjN2YxN2NkZDk1NyIsIm9wZXJhdG9yIjoib3AifQ.e3vqf3bIDqT9K89XA8HFHlJEplnKuOpvix7KaIBupPI

```
Expected Go assertions: recompute entry_hash for each of the 3 events (hash1=ab31ec45...3183, hash2=d1dd0137...df52, hash3=b0219c32...624e == head), prev linkage, then mutate payload / swap events / delete middle event and assert verify_chain reports first_bad_seq and the reasons "an event was modified" / "an event is missing, reordered or replaced". Also add a non-chained legacy line (hash "") -> unchained count. Also feed a Go-produced chain back through Python verify_chain (round trip) in CI, since Python is the verifier of record.
B2 sample (inputs {"to":"a@b.com","amount":12.5,"note":"café"}): inputs_sha256 = 62751459e9aad0bd4ea583f1c4126b39cb68e6691a798d94d5134cf4fcba7ffb (in the output above; approval hash uses ensure_ascii=True).
