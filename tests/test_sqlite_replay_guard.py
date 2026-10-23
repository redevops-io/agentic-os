"""Approval nonces stay single-use across restarts and processes (audit C3/F10)."""
import multiprocessing as mp
import os
import stat

import pytest

from agentic_os.mission.approval import ReplayGuard, SqliteReplayGuard, default_replay_guard


def test_first_use_wins_and_replay_is_refused(tmp_path):
    g = SqliteReplayGuard(str(tmp_path / "r.db"))
    assert g.consume("n1", exp=2000.0, now=1000.0) is True
    assert g.consume("n1", exp=2000.0, now=1001.0) is False
    assert g.consume("n2", exp=2000.0, now=1001.0) is True


def test_a_restart_does_not_forget_a_used_nonce(tmp_path):
    path = str(tmp_path / "r.db")
    SqliteReplayGuard(path).consume("n1", exp=2000.0, now=1000.0)
    again = SqliteReplayGuard(path)                           # a new process / restarted operator
    assert again.consume("n1", exp=2000.0, now=1500.0) is False


def test_expired_nonces_are_purged_so_the_table_stays_bounded(tmp_path):
    g = SqliteReplayGuard(str(tmp_path / "r.db"), max_entries=2)
    assert g.consume("a", exp=1100.0, now=1000.0) and g.consume("b", exp=1100.0, now=1000.0)
    assert g.consume("c", exp=1100.0, now=1000.0) is False     # full of live nonces: fail closed
    assert g.consume("c", exp=3000.0, now=1200.0) is True       # a and b expired and were purged


def test_the_file_is_private(tmp_path):
    p = tmp_path / "r.db"
    SqliteReplayGuard(str(p))
    assert stat.S_IMODE(os.stat(p).st_mode) & 0o077 == 0


def _worker(path, q):
    g = SqliteReplayGuard(path)
    q.put(g.consume("shared", exp=9_999_999_999.0, now=1.0))


def test_two_processes_cannot_both_consume_the_same_nonce(tmp_path):
    path = str(tmp_path / "r.db")
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(path, q)) for _ in range(4)]
    [p.start() for p in procs]
    [p.join(30) for p in procs]
    results = [q.get(timeout=5) for _ in procs]
    assert results.count(True) == 1 and results.count(False) == 3


def test_default_is_per_process_unless_a_db_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_REPLAY_DB", raising=False)
    assert isinstance(default_replay_guard(), ReplayGuard)
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "x.db"))
    assert isinstance(default_replay_guard(), SqliteReplayGuard)


def test_an_operator_uses_the_durable_guard_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "op.db"))
    from agentic_os.mission.operator_sdk import Operator
    assert isinstance(Operator("x", []).replay_guard, SqliteReplayGuard)
