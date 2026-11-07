"""The event log has a tail anchor: cutting records off the END of the log is detected.

The hash chain already detects an edited or mid-chain-deleted record, but a shorter chain is still a valid chain. The store keeps a
second durable record (``<log>.anchor`` by default): record count, last sequence, running head hash, optionally with an HMAC. It is
written atomically after every append and checked when the log is opened and before a mission is rebuilt from it.

These tests also pin what the anchor does NOT do: a writer who can rewrite both the log and the anchor can make them agree again."""
from __future__ import annotations

import json
import os

import pytest

from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor
from agentic_os.mission.integrity import AnchorFile, LogIntegrityError
from agentic_os.mission.receipt import mission_receipt
from agentic_os.mission.runtime import MissionRuntime, ReplayError
from agentic_os.mission.store import EventStore, entry_hash


def fill(store, n=6):
    for i in range(n):
        store.append("Tick", f"m{i % 2}", {"i": i})
    return store


def lines(path):
    return open(path).read().splitlines()


def write_lines(path, ls):
    with open(path, "w") as fh:
        fh.write("\n".join(ls) + ("\n" if ls else ""))


def rechain(ls):
    heads, out = {}, []
    for ln in ls:
        d = json.loads(ln)
        d["prev"] = heads.get(d["mission_id"], "")
        d["hash"] = entry_hash(d["prev"], d["seq"], d["ts"], d["type"], d["mission_id"], d["payload"])
        heads[d["mission_id"]] = d["hash"]
        out.append(json.dumps(d))
    return out


# ── the anchor is written with every append ──────────────────────────────────────────────────────────────────────────
def test_every_append_updates_the_anchor(tmp_path):
    path = str(tmp_path / "log.jsonl")
    store = EventStore(path)
    for n in range(1, 4):
        store.append("Tick", "m", {"n": n})
        a = AnchorFile(path + ".anchor").read()
        assert a["count"] == n and a["last_seq"] == n and a["head"] == store.log_head()
    assert EventStore(path).integrity.status == "ok"


def test_a_reopened_store_keeps_extending_the_same_anchor(tmp_path):
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path), 3)
    s2 = EventStore(path)
    s2.append("Tick", "m0", {"i": 99})
    assert AnchorFile(path + ".anchor").read()["count"] == 4 and EventStore(path).integrity.status == "ok"


def test_two_stores_on_one_log_keep_one_consistent_anchor(tmp_path):
    path = str(tmp_path / "log.jsonl")
    a, b = EventStore(path), EventStore(path)
    for i in range(6):
        (a if i % 2 else b).append("Tick", "m", {"i": i})
    assert AnchorFile(path + ".anchor").read()["count"] == 6 and EventStore(path).integrity.status == "ok"


# ── tail truncation ──────────────────────────────────────────────────────────────────────────────────────────────────
def test_cutting_records_off_the_end_of_the_log_is_detected_on_open(tmp_path):
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path), 6)
    write_lines(path, lines(path)[:-2])                                    # a valid, shorter chain
    with pytest.raises(LogIntegrityError, match="truncated"):
        EventStore(path)


def test_deleting_the_whole_log_is_detected(tmp_path):
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path), 3)
    os.unlink(path)
    with pytest.raises(LogIntegrityError, match="truncated"):
        EventStore(path)


def test_a_truncated_log_can_be_opened_for_inspection_but_not_extended(tmp_path):
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path), 6)
    write_lines(path, lines(path)[:-2])
    s = EventStore(path, strict=False)
    assert s.integrity.status == "truncated" and not s.integrity.ok and len(s.all()) == 4
    with pytest.raises(LogIntegrityError):
        s.append("Tick", "m0", {"i": 1})
    assert len(lines(path)) == 4                                           # nothing was written
    s.reanchor()                                                           # an explicit operator decision to accept the log as it is
    s.append("Tick", "m0", {"i": 1})
    assert EventStore(path).integrity.status == "ok"


def test_a_mission_is_not_rebuilt_from_a_truncated_log(tmp_path):
    path = str(tmp_path / "log.jsonl")
    reg, client = build_fleet()
    rt = MissionRuntime(reg, Executor(client), store=EventStore(path))
    m = rt.create_mission("Onboard a new customer", policy_refs=["billing:write", "support:write", "books:write", "compliance:write"], template="onboarding")
    rt.run(m.id)
    write_lines(path, lines(path)[:-3])
    rt2 = MissionRuntime(reg, Executor(client), store=EventStore(path, strict=False))
    with pytest.raises(ReplayError, match="integrity"):
        rt2.rehydrate(m.id)
    assert m.id not in rt2._missions


# ── mid-log edits ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_an_edit_in_the_middle_of_the_log_is_detected_by_the_chain(tmp_path):
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path), 6)
    ls = lines(path)
    d = json.loads(ls[2])
    d["payload"]["i"] = 999
    ls[2] = json.dumps(d)
    write_lines(path, ls)
    s = EventStore(path)
    rep = s.check_integrity()
    assert not rep.ok and rep.status == "chain" and "modified" in rep.reason


def test_an_edit_that_recomputes_the_chain_is_detected_by_the_anchor(tmp_path):
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path), 6)
    ls = lines(path)
    d = json.loads(ls[2])
    d["payload"]["i"] = 999
    ls[2] = json.dumps(d)
    write_lines(path, rechain(ls))                                         # a valid chain again, with different content
    assert EventStore(path, strict=False).verify().ok                      # the chain alone cannot tell
    with pytest.raises(LogIntegrityError, match="mismatch|rewritten"):
        EventStore(path)


# ── the anchor itself ────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_log_ahead_of_its_anchor_is_not_an_error_and_the_next_append_catches_up(tmp_path):
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path), 3)
    EventStore(path, anchor=False).append("Tick", "m0", {"i": 7})          # the writer stopped between the append and the anchor
    s = EventStore(path)
    assert s.integrity.status == "lagging" and s.integrity.ok
    s.append("Tick", "m0", {"i": 8})
    assert AnchorFile(path + ".anchor").read()["count"] == 5 and EventStore(path).integrity.status == "ok"


def test_a_log_with_no_anchor_is_trusted_on_first_use(tmp_path):
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path, anchor=False), 3)
    s = EventStore(path)
    assert s.integrity.status == "unanchored" and s.integrity.ok
    s.append("Tick", "m0", {"i": 1})
    assert AnchorFile(path + ".anchor").read()["count"] == 4


def test_an_unreadable_or_malformed_anchor_is_refused(tmp_path):
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path), 3)
    for junk in ("not json", json.dumps({"version": 1}), json.dumps({"version": 1, "count": "3", "head": "x"})):
        open(path + ".anchor", "w").write(junk)
        with pytest.raises(LogIntegrityError, match="anchor"):
            EventStore(path)


def test_the_anchor_can_live_somewhere_else(tmp_path):
    path, apath = str(tmp_path / "log.jsonl"), str(tmp_path / "elsewhere" / "head.json")
    os.makedirs(os.path.dirname(apath))
    fill(EventStore(path, anchor_path=apath), 4)
    assert not os.path.exists(path + ".anchor") and AnchorFile(apath).read()["count"] == 4
    write_lines(path, lines(path)[:-1])
    with pytest.raises(LogIntegrityError, match="truncated"):
        EventStore(path, anchor_path=apath)


# ── an optional key ──────────────────────────────────────────────────────────────────────────────────────────────────
def test_with_a_key_the_anchor_carries_a_mac_and_an_anchor_without_the_key_is_refused(tmp_path):
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path, anchor_key=b"k" * 32), 3)
    assert "mac" in AnchorFile(path + ".anchor").read()
    EventStore(path, anchor_key=b"k" * 32)                                 # the right key opens it
    with pytest.raises(LogIntegrityError, match="MAC|key"):
        EventStore(path, anchor_key=b"x" * 32)


def test_with_a_key_an_edited_anchor_is_refused(tmp_path):
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path, anchor_key=b"k" * 32), 3)
    a = json.loads(open(path + ".anchor").read())
    a["count"] = 1                                                         # pretend the log never got further
    open(path + ".anchor", "w").write(json.dumps(a))
    with pytest.raises(LogIntegrityError):
        EventStore(path, anchor_key=b"k" * 32)


def test_the_key_comes_from_the_environment_when_not_passed(tmp_path, monkeypatch):
    path = str(tmp_path / "log.jsonl")
    monkeypatch.setenv("AGENTIC_OS_ANCHOR_KEY", "s" * 32)
    fill(EventStore(path), 2)
    assert "mac" in AnchorFile(path + ".anchor", key=b"s" * 32).read()


# ── the limit, stated as a test ──────────────────────────────────────────────────────────────────────────────────────
def test_a_writer_who_can_rewrite_both_the_log_and_the_anchor_is_not_detected(tmp_path):
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path), 6)
    write_lines(path, lines(path)[:-2])
    short = EventStore(path, strict=False)
    short.reanchor()                                                       # what such a writer does with the anchor
    again = EventStore(path)
    assert again.integrity.status == "ok" and len(again.all()) == 4        # the truncation is now invisible to the store itself


def test_without_the_key_a_forged_anchor_is_accepted_with_the_key_it_is_not(tmp_path):
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path, anchor_key=b"k" * 32), 6)
    write_lines(path, lines(path)[:-2])
    EventStore(path, strict=False, anchor_key=b"k" * 32)                   # truncated and caught
    forger = EventStore(path, strict=False, anchor=False)
    forged = {"count": len(forger.all()), "last_seq": forger.all()[-1].seq, "head": forger.log_head()}
    AnchorFile(path + ".anchor").write(forged, fsync=False)                # a forger without the key writes a plain anchor
    EventStore(path)                                                       # no key configured: accepted
    with pytest.raises(LogIntegrityError):
        EventStore(path, anchor_key=b"k" * 32)                             # key configured: refused


# ── the receipt shows the anchor ─────────────────────────────────────────────────────────────────────────────────────
def test_the_mission_receipt_reports_the_anchor_status(tmp_path):
    path = str(tmp_path / "log.jsonl")
    reg, client = build_fleet()
    rt = MissionRuntime(reg, Executor(client), store=EventStore(path))
    m = rt.create_mission("Onboard a new customer", policy_refs=["billing:write", "support:write", "books:write", "compliance:write"], template="onboarding")
    rt.run(m.id)
    r = mission_receipt(rt, m.id)
    assert r["ledger_anchor"]["status"] == "ok" and r["ledger_anchor"]["anchored"] == len(rt.store.all())


# ── a concurrent writer is not truncation ────────────────────────────────────────────────────────────────────────────
def test_an_append_by_another_process_between_reading_the_log_and_the_anchor_is_not_truncation(tmp_path, monkeypatch):
    """Another process appends the event first and moves the anchor second. A reader that reads the log and THEN the anchor can see a
    log of 3 and an anchor of 4 and call it truncation; the store reads the anchor first."""
    path = str(tmp_path / "log.jsonl")
    fill(EventStore(path), 3)
    other = EventStore(path)
    real = EventStore._read_tail
    fired = []

    def racing(self):
        out = real(self)
        if not fired:
            fired.append(1)
            other.append("Tick", "m0", {"i": 100})       # the other process finishes an append (log, then anchor) right after our read
        return out
    monkeypatch.setattr(EventStore, "_read_tail", racing)
    s = EventStore(path)                                   # must not raise
    assert s.integrity.ok
    fired.clear()
    assert s.check_integrity().ok
    monkeypatch.setattr(EventStore, "_read_tail", real)
    assert EventStore(path).integrity.status == "ok"
