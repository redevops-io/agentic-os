"""The audit chain, the canonical JSON and the receipt are byte-identical on every OS.

The committed fixtures in ``tests/golden/`` were produced once (``python tests/test_audit_chain_portability.py --write-golden``) and
are read with ``read_bytes()`` here, so a checkout that rewrote their line endings, a platform whose ``json`` or ``repr`` of a float
differed, or a writer that translated newlines would fail these tests on that OS. Timestamps are epoch seconds (UTC, a float): no
local time zone enters any hash. A path-like value in a payload is NOT covered: ``default=str`` would render it with the platform's
separator, so callers record text, not ``Path`` objects (listed in the pull request as to implement)."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_os.mission.store import EventStore, MissionRepository, _canonical_entry, entry_hash, verify_chain

GOLDEN = Path(__file__).resolve().parent / "golden"
T0 = 1_700_000_000.0

# Fixed inputs: every JSON feature the chain's canonical form has to agree on across platforms.
EVENTS = [
    ("MissionCreated", "m-1", {"goal": "pay the invoice", "zeta": 1, "alpha": {"b": [1, 2.5, None], "a": True}}),
    ("MissionCreated", "m-2", {"goal": "café 日本語 \U0001F600", "note": "line1\r\nline2\ttab"}),
    ("NodeDispatched", "m-1", {"node_id": "n1", "n": 1.0, "big": 1e16, "small": 0.1, "neg": -0.0, "int": 10 ** 18}),
    ("NodeSucceeded", "m-1", {"node_id": "n1", "result": {"amount": 100, "currency": "EUR", "tags": ["é", "é"]}}),
    ("ApprovalGranted", "m-1", {"node_id": "n1", "capability": "pay", "approver": "approver:acme", "inputs_sha256": "ab" * 32}),
    ("MissionOutcome", "m-1", {"kind": "done", "success": True, "business_value": 12.5}),
]


def _write_log(path: Path, clock) -> EventStore:
    real = time.time
    time.time = clock
    try:
        store = EventStore(str(path))
        for t, m, p in EVENTS:
            store.append(t, m, p)
    finally:
        time.time = real
    return store


def _fake_runtime(store: EventStore):
    return SimpleNamespace(store=store, repo=MissionRepository(store), evidence=SimpleNamespace(ledger=lambda mid: []))


def _receipt_bytes(store: EventStore) -> bytes:
    from agentic_os.mission.receipt import mission_receipt
    return (json.dumps(mission_receipt(_fake_runtime(store), "m-1"), sort_keys=True, indent=1) + "\n").encode("utf-8")


def _canonical_cases() -> dict:
    return {
        "ascii": _canonical_entry(1, T0, "T", "m", {"b": 1, "a": 2}),
        "unicode": _canonical_entry(2, T0 + 0.25, "T", "m", {"s": "café 日本語 \U0001F600"}),
        "floats": _canonical_entry(3, 1.5, "T", "m", {"a": 1.0, "b": 1e16, "c": 0.1, "d": 1e-7, "e": 123456789.123456789}),
        "controls": _canonical_entry(4, T0, "T", "m", {"s": "a\r\nb\tc\x00d e"}),
        "nested_order": _canonical_entry(5, T0, "T", "m", {"z": {"y": 1, "x": [3, 2, 1]}, "a": None}),
    }


def _clock():
    n = {"i": 0}

    def tick():
        n["i"] += 1
        return T0 + 0.25 * n["i"]
    return tick


def write_golden() -> None:
    GOLDEN.mkdir(exist_ok=True)
    log = GOLDEN / "event_log.jsonl"
    if log.exists():
        log.unlink()
    store = _write_log(log, _clock())
    (GOLDEN / "mission_receipt.json").write_bytes(_receipt_bytes(store))
    cases = {k: {"bytes_hex": v.hex(), "sha256": __import__("hashlib").sha256(v).hexdigest()} for k, v in _canonical_cases().items()}
    heads = {m: store.head(m) for m in ("m-1", "m-2")}
    (GOLDEN / "canonical_entries.json").write_bytes((json.dumps({"canonical": cases, "heads": heads}, indent=1, sort_keys=True) + "\n").encode("utf-8"))


def _canon():
    return json.loads((GOLDEN / "canonical_entries.json").read_bytes().decode("utf-8"))


def test_the_golden_files_are_lf_only():
    for name in ("event_log.jsonl", "mission_receipt.json", "canonical_entries.json"):
        data = (GOLDEN / name).read_bytes()
        assert b"\r" not in data and data.endswith(b"\n"), name        # a CRLF checkout would be caught here, not by a hash


def test_the_writer_produces_the_committed_log_byte_for_byte(tmp_path):
    p = tmp_path / "log.jsonl"
    _write_log(p, _clock())
    assert p.read_bytes() == (GOLDEN / "event_log.jsonl").read_bytes()


def test_the_committed_log_loads_verifies_and_has_the_committed_heads():
    store = EventStore(str(GOLDEN / "event_log.jsonl"))
    rep = verify_chain(store.all())
    assert rep.ok and rep.checked == len(EVENTS) and rep.unchained == 0
    for m, head in _canon()["heads"].items():
        assert store.head(m) == head


def test_canonical_entry_bytes_and_hashes_are_identical_everywhere():
    import hashlib
    want = _canon()["canonical"]
    for name, got in _canonical_cases().items():
        assert got.hex() == want[name]["bytes_hex"], name
        assert hashlib.sha256(got).hexdigest() == want[name]["sha256"], name
    # and the chain hash is sha256(prev_ascii + "|" + canonical)
    c = _canonical_cases()["ascii"]
    assert entry_hash("p" * 4, 1, T0, "T", "m", {"b": 1, "a": 2}) == __import__("hashlib").sha256(b"pppp|" + c).hexdigest()


def test_the_receipt_is_identical_everywhere():
    store = EventStore(str(GOLDEN / "event_log.jsonl"))
    assert _receipt_bytes(store) == (GOLDEN / "mission_receipt.json").read_bytes()


def test_a_log_whose_lines_were_rewritten_to_crlf_still_loads_and_the_next_append_is_lf(tmp_path):
    """Explicit newline handling: a log that passed through a CRLF-converting tool (an editor, an autocrlf checkout) is read by its
    parsed records, so the chain still verifies; new records are always written with a bare LF."""
    raw = (GOLDEN / "event_log.jsonl").read_bytes()
    p = tmp_path / "crlf.jsonl"
    p.write_bytes(raw.replace(b"\n", b"\r\n"))
    store = EventStore(str(p))
    assert verify_chain(store.all()).ok
    assert {m: store.head(m) for m in ("m-1", "m-2")} == _canon()["heads"]
    store.append("Later", "m-1", {"n": 1})
    tail = p.read_bytes()[len(raw.replace(b"\n", b"\r\n")):]
    assert tail.endswith(b"\n") and b"\r" not in tail
    assert verify_chain(EventStore(str(p)).all()).ok


def test_timestamps_are_epoch_seconds_in_utc_and_no_time_zone_enters_a_hash(tmp_path):
    p = tmp_path / "tz.jsonl"
    before = time.time()
    store = EventStore(str(p))
    ev = store.append("T", "m", {})
    after = time.time()
    assert before <= ev.ts <= after                                    # an epoch float, not local time
    assert datetime.fromtimestamp(ev.ts, timezone.utc).tzinfo is timezone.utc
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset exists on POSIX only; the hash takes no local-time input on any OS (the epoch float above)")
    old = os.environ.get("TZ")
    try:
        heads = []
        for tz in ("UTC", "Asia/Tokyo", "America/Los_Angeles"):
            os.environ["TZ"] = tz
            time.tzset()
            q = tmp_path / f"{tz.replace('/', '_')}.jsonl"
            heads.append(_write_log(q, _clock()).head("m-1"))
        assert len(set(heads)) == 1 and heads[0] == _canon()["heads"]["m-1"]
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-golden"]:
        write_golden()
    else:
        print(__doc__)
