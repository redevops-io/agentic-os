#!/usr/bin/env python3
"""Generate the shared conformance vectors for the security/execution behaviours (Python is the reference).

    python conformance/gen_vectors.py            # rewrite conformance/vectors/*.json
    python conformance/gen_vectors.py --check    # exit 1 if the committed vectors are stale (CI/tests use this)

Every port (Go, Kotlin, .NET) must assert these files byte for byte. Numbers matter: payloads are written with
``json.dumps`` so int vs float and float repr are preserved in the fixture text; a port must decode with its
number-preserving mode (Go ``UseNumber``) rather than a default that turns every number into a double.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from agentic_os.mission.approval import action_summary_of, approval_expired, inputs_hash  # noqa: E402
from agentic_os.mission.assurance import assurance_findings  # noqa: E402
from agentic_os.mission.store import Event, _canonical_entry, entry_hash, verify_chain  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vectors")


def dump(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=False, ensure_ascii=False) + "\n"


def chain_vectors():
    payload_sets = {
        "ascii": {"goal": "pay invoice", "n": 3},
        "unicode": {"naïve": "é中😀", "emoji": "😀", "mixed": "aé中\U0001F600z"},
        # code-point order vs UTF-16 order: U+FFFF sorts BEFORE U+1F600 by code point but AFTER it by UTF-16 code unit
        "key_order_astral": {"\U0001F600": 1, "￿": 2, "z": 3, "é": 4, "A": 5},
        "numbers": {"int0": 0, "neg": -1, "big": 12345678901234567890, "f1": 1.0, "f01": 0.1, "f16": 1e16,
                    "f15": 123456789012345.0, "fe5": 1e-05, "f4": 0.0001, "f300": 1.5e300, "negzero": -0.0,
                    "f100": 100.0, "f22": 1e22, "list": [1, 2.5, [3.0, {"k": 1e-07}]]},
        "escapes": {"s": "q\"b\\s\n\r\t\b\f\u0001\u007f  /"},
        "types": {"t": True, "f": False, "n": None, "e": {}, "l": [], "nested": {"b": {"a": [None, True]}}},
    }
    ts_values = [1792774215.854218, 1.0, 1e9, 0.5, 1700000000.0000001]
    cases = []
    for name, payload in payload_sets.items():
        events, prev_by_mission = [], {}
        for i, ts in enumerate(ts_values[:3], start=1):
            mission = "m1" if i != 2 else "m2"
            prev = prev_by_mission.get(mission, "")
            h = entry_hash(prev, i, ts, "NodeSucceeded", mission, payload)
            events.append({"seq": i, "ts": ts, "type": "NodeSucceeded", "mission_id": mission, "payload": payload,
                           "prev": prev, "hash": h,
                           "canonical": _canonical_entry(i, ts, "NodeSucceeded", mission, payload).decode("utf-8")})
            prev_by_mission[mission] = h
        cases.append({"name": name, "events": events, "ok": True})
    return {
        "doc": "hash = sha256(prev + '|' + canonical) where canonical = json.dumps({seq,ts,type,mission_id,payload}, "
               "sort_keys=True, separators=(',',':'), ensure_ascii=False) in UTF-8; prev is the previous event's hash "
               "OF THE SAME MISSION ('' for its first). A port must reproduce 'canonical' and 'hash' exactly.",
        "cases": cases,
        "tamper": tamper_cases(),
    }


def tamper_cases():
    def mk(n=3, mission="m"):
        evs, prev = [], ""
        for i in range(1, n + 1):
            payload = {"i": i, "amount": 100}
            h = entry_hash(prev, i, float(1000 + i), "E", mission, payload)
            evs.append({"seq": i, "ts": float(1000 + i), "type": "E", "mission_id": mission, "payload": payload,
                        "prev": prev, "hash": h})
            prev = h
        return evs
    base = mk()
    edited = json.loads(json.dumps(base)); edited[1]["payload"]["amount"] = 1
    deleted = [base[0], base[2]]
    reordered = [base[0], base[2], base[1]]

    def report(evs):
        objs = [Event(type=e["type"], mission_id=e["mission_id"], payload=e["payload"], seq=e["seq"], ts=e["ts"],
                      prev=e["prev"], hash=e["hash"]) for e in evs]
        r = verify_chain(objs)
        return {"ok": r.ok, "checked": r.checked, "unchained": r.unchained, "first_bad_seq": r.first_bad_seq,
                "reason": r.reason}
    legacy_then_chained = [{"seq": 1, "ts": 1.0, "type": "E", "mission_id": "m", "payload": {}, "prev": "", "hash": ""}]
    for e in mk(2):
        e = dict(e); e["seq"] += 1; legacy_then_chained.append(e)
    # the chained part starts after a legacy event: its first prev is "" (legacy events do not extend the chain)
    return [
        {"name": "clean", "events": base, "expect": report(base)},
        {"name": "edited", "events": edited, "expect": report(edited)},
        {"name": "deleted", "events": deleted, "expect": report(deleted)},
        {"name": "reordered", "events": reordered, "expect": report(reordered)},
        {"name": "legacy_prefix", "events": legacy_then_chained, "expect": report(legacy_then_chained)},
    ]


def inputs_vectors():
    cases = [
        {"name": "simple", "inputs": {"account_id": "A1", "amount": 100}},
        {"name": "strips_approval_and_meta_keys", "inputs": {"a": 1, "_approval": {"approved": True}, "_approval_x": 1,
                                                              "_mission": "m", "_goal": "g", "_node": "n"}},
        {"name": "nested_approval_key_kept", "inputs": {"a": {"_approval": 1, "b": 2}}},
        {"name": "unicode_ensure_ascii", "inputs": {"naïve": "é中😀", "k\U0001F600": 1}},
        {"name": "numbers", "inputs": {"i": 5, "f": 10.0, "big": 12345678901234567890, "e": 1e-07, "neg0": -0.0,
                                       "l": [1, 2.0]}},
        {"name": "escapes", "inputs": {"s": "\b\f\n\r\t\u0001\u007f "}},
    ]
    for c in cases:
        c["inputs_sha256"] = inputs_hash(c["inputs"])
    return {"doc": "inputs_sha256 = sha256(json.dumps(strip_approval_keys(top-level inputs minus _mission/_goal/_node/_approval*), "
                   "sort_keys=True, separators=(',',':')) with DEFAULT ensure_ascii=True)", "cases": cases}


def summary_vectors():
    cases = [
        {"name": "allowlist_only", "resolved": {"amount": 100, "currency": "USD", "secret": "x", "api_key": "k",
                                                 "_goal": "g", "account_id": "A1"}},
        {"name": "email_masked", "resolved": {"to": "jane.doe@acme.com", "recipient": "al@x.io", "subject": "a@b@c"}},
        {"name": "truncation", "resolved": {"subject": "x" * 100, "destination": "é" * 90}},
        {"name": "non_scalars", "resolved": {"resource": {"a": 1}, "count": [1, 2], "order_id": None, "total": 12.5,
                                              "quantity": True}},
        {"name": "missing_keys_skipped", "resolved": {"foo": 1}},
        {"name": "email_not_at_start", "resolved": {"to": "contact: jane@acme.com or bob@evil.org"}},
    ]
    for c in cases:
        c["summary"] = action_summary_of(c["resolved"])
    return {"doc": "allowlist (SUMMARY_KEYS), emails masked ([^@\\s]{1,2})[^@\\s]*@ -> \\1***@, strings cut to 80 "
                   "characters (code points), other types str() cut to 80; numbers/bools kept", "cases": cases}


def ttl_vectors():
    rows = [(1000.0, 1050.0, 60.0), (1000.0, 1061.0, 60.0), (1000.0, 1060.0, 60.0), (1000.0, 9999999.0, 0.0),
            (None, 5000.0, 60.0), (1000.0, 500.0, 60.0)]
    return {"doc": "expired iff ttl_s > 0 and parked_at is known and now - parked_at > ttl_s",
            "cases": [{"parked_at": p, "now": n, "ttl_s": t, "expired": approval_expired(p, n, t)} for p, n, t in rows]}


def assurance_vectors():
    def ev(seq, typ, mission, payload, ts):
        return {"seq": seq, "ts": ts, "type": typ, "mission_id": mission, "payload": payload, "prev": "", "hash": ""}
    scenarios = {
        "healthy": ([ev(1, "NodeParked", "m", {"node_id": "n"}, 100.0), ev(2, "ApprovalGranted", "m", {"node_id": "n"}, 101.0),
                     ev(3, "NodeDispatched", "m", {"node_id": "n"}, 102.0), ev(4, "VerificationRecorded", "m", {"node_id": "n"}, 103.0),
                     ev(5, "NodeSucceeded", "m", {"node_id": "n", "capability": "billing.dunning"}, 104.0)], 110.0, {}),
        "stuck_gate": ([ev(1, "NodeParked", "m", {"node_id": "n"}, 100.0)], 100.0 + 90000, {}),
        "dispatch_without_result": ([ev(1, "NodeDispatched", "m", {"node_id": "n"}, 100.0)], 100.0 + 2000, {}),
        "unverified_success": ([ev(1, "NodeSucceeded", "m", {"node_id": "n", "capability": "social.publish"}, 100.0)], 100.0,
                               {"side_effecting": ["social.publish"]}),
        "unverified_success_not_side_effecting": ([ev(1, "NodeSucceeded", "m", {"node_id": "n", "capability": "crm.read"}, 100.0)], 100.0,
                                                  {"side_effecting": ["social.publish"]}),
        "repeated_failures": ([ev(i, "NodeFailed", "m", {"node_id": f"n{i}", "capability": "erpnext.write"}, 100.0 + i) for i in (1, 2, 3)],
                              200.0, {}),
        "invalidated_and_expired": ([ev(1, "ApprovalInvalidated", "m", {"node_id": "n", "reason": "inputs changed since approval"}, 100.0),
                                     ev(2, "ApprovalExpired", "m", {"node_id": "n", "ttl_s": 60}, 101.0)], 200.0, {}),
        "two_missions": ([ev(1, "NodeParked", "a", {"node_id": "n"}, 100.0), ev(2, "NodeDispatched", "b", {"node_id": "n"}, 100.0)],
                         100.0 + 90000, {}),
    }
    cases = []
    for name, (events, now, opts) in scenarios.items():
        objs = [Event(type=e["type"], mission_id=e["mission_id"], payload=e["payload"], seq=e["seq"], ts=e["ts"],
                      prev=e["prev"], hash=e["hash"]) for e in events]
        kw = {"now": now}
        if "side_effecting" in opts:
            kw["side_effecting"] = set(opts["side_effecting"])
        findings = assurance_findings(objs, **kw)
        # output order is part of the contract: sorted by (seq, kind, node_id) so ports need not match emission order
        rows = sorted(({"kind": f.kind, "severity": f.severity, "mission_id": f.mission_id, "node_id": f.node_id,
                        "detail": f.detail, "seq": f.seq} for f in findings),
                      key=lambda r: (r["seq"], r["kind"], r["node_id"]))
        cases.append({"name": name, "now": now, "options": opts, "events": events, "findings": rows})
    return {"doc": "defaults: gate_after_s=86400, dispatch_after_s=900, failures=3, window_s=3600. Findings compared as a "
                   "set sorted by (seq, kind, node_id). A failed hash chain adds a critical ledger_tampered finding.",
            "cases": cases}


FILES = {"chain.json": chain_vectors, "inputs_digest.json": inputs_vectors, "action_summary.json": summary_vectors,
         "approval_ttl.json": ttl_vectors, "assurance.json": assurance_vectors}


def main(check: bool) -> int:
    os.makedirs(OUT, exist_ok=True)
    stale = []
    for name, fn in FILES.items():
        text = dump(fn())
        path = os.path.join(OUT, name)
        if check:
            if not os.path.exists(path) or open(path, encoding="utf-8").read() != text:
                stale.append(name)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
    if stale:
        print("stale conformance vectors:", ", ".join(stale), "(run: python conformance/gen_vectors.py)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--check" in sys.argv))
