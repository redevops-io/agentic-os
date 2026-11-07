"""A reader that keeps a JSONL event log open notices when the file becomes SHORTER than what it has already read (truncated or replaced
behind it): its integrity check says so instead of reporting the stale in-memory records as an intact log, and it refuses to append at
the old offset. A log that only grew is unaffected."""
from __future__ import annotations

import pytest

from agentic_os.mission.integrity import IntegrityError
from agentic_os.mission.store import EventStore


def make(tmp_path, n=4):
    p = str(tmp_path / "ev.jsonl")
    w = EventStore(path=p)
    for i in range(n):
        w.append("MissionCreated" if i == 0 else "NodeSucceeded", "m1", {"i": i})
    return p, EventStore(path=p)


def cut_last(p):
    lines = open(p, "rb").read().splitlines(keepends=True)
    open(p, "wb").write(b"".join(lines[:-1]))


def test_a_log_cut_behind_an_open_reader_is_reported_truncated(tmp_path):
    p, r = make(tmp_path)
    assert r.check_anchor().status == "ok"
    cut_last(p)
    a = r.check_anchor()
    assert (a.ok, a.status) == (False, "truncated")
    i = r.check_integrity("m1")
    assert (i.ok, i.status) == (False, "truncated")


def test_the_reader_does_not_append_after_the_file_shrank(tmp_path):
    p, r = make(tmp_path)
    cut_last(p)
    r.check_anchor()
    with pytest.raises(IntegrityError):
        r.append("NodeSucceeded", "m1", {"i": 99})


def test_a_log_that_only_grew_is_still_fine(tmp_path):
    p, r = make(tmp_path)
    EventStore(path=p).append("NodeSucceeded", "m1", {"i": 4})
    assert r.check_anchor().ok and r.check_integrity("m1").ok
    assert len(r.for_mission("m1")) == 5            # the check itself picked up what the other writer appended
    assert r.check_anchor().status == "ok"


def test_without_an_anchor_a_shrunk_file_is_still_reported(tmp_path):
    p = str(tmp_path / "ev.jsonl")
    w = EventStore(path=p, anchor=False)
    for i in range(3):
        w.append("MissionCreated" if i == 0 else "NodeSucceeded", "m1", {"i": i})
    r = EventStore(path=p, anchor=False)
    cut_last(p)
    assert r.check_anchor().status == "truncated"
