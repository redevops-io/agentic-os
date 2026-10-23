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
    procs = [ctx.Process(target=_worker, args=(path, q)) for _ in range(8)]
    [p.start() for p in procs]
    [p.join(120) for p in procs]          # a cold spawned interpreter imports the kernel: allow for it
    results = [q.get(timeout=60) for _ in procs]
    assert results.count(True) == 1 and results.count(False) == 7


def test_default_is_per_process_unless_a_db_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_REPLAY_DB", raising=False)
    assert isinstance(default_replay_guard(), ReplayGuard)
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "x.db"))
    assert isinstance(default_replay_guard(), SqliteReplayGuard)


def test_an_operator_uses_the_durable_guard_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "op.db"))
    from agentic_os.mission.operator_sdk import Operator
    assert isinstance(Operator("x", []).replay_guard, SqliteReplayGuard)


def test_production_mode_refuses_to_start_without_a_durable_store(monkeypatch):
    from agentic_os.mission.approval import ReplayProtectionNotDurable
    from agentic_os.mission.operator_sdk import Operator
    monkeypatch.delenv("AGENTIC_OS_REPLAY_DB", raising=False)
    monkeypatch.setenv("AGENTIC_OS_REQUIRE_DURABLE_REPLAY", "1")
    with pytest.raises(ReplayProtectionNotDurable):
        default_replay_guard()
    with pytest.raises(ReplayProtectionNotDurable):
        Operator("x", [])                                            # an operator never starts per-process by accident


def test_production_mode_with_a_db_starts_normally(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_OS_REQUIRE_DURABLE_REPLAY", "1")
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "r.db"))
    assert isinstance(default_replay_guard(), SqliteReplayGuard)


def test_without_the_flag_the_fallback_is_per_process_but_logged_once(monkeypatch, caplog):
    import agentic_os.mission.approval as appr
    monkeypatch.delenv("AGENTIC_OS_REPLAY_DB", raising=False)
    monkeypatch.delenv("AGENTIC_OS_REQUIRE_DURABLE_REPLAY", raising=False)
    monkeypatch.setattr(appr, "_WARNED_NON_DURABLE", False)
    with caplog.at_level("WARNING", logger="agentic_os.replay_guard"):
        g = default_replay_guard()
        default_replay_guard()
    assert isinstance(g, ReplayGuard) and caplog.text.count("per-process") == 1


def test_a_locked_database_is_retried_not_mistaken_for_a_replay_or_a_crash():
    import sqlite3
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"
    assert SqliteReplayGuard._retry(flaky, delay=0) == "ok" and calls["n"] == 3

    def broken():
        raise sqlite3.OperationalError("no such table: used_nonces")
    with pytest.raises(sqlite3.OperationalError):                       # a real error is not retried
        SqliteReplayGuard._retry(broken, delay=0)

    def always_locked():
        raise sqlite3.OperationalError("database is locked")
    with pytest.raises(sqlite3.OperationalError):                       # bounded: it does not spin forever
        SqliteReplayGuard._retry(always_locked, attempts=3, delay=0)
