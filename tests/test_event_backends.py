"""Event-store backend conformance — memory / JSONL / DuckDB are interchangeable.

One behavioural suite run against every offline backend, proving they satisfy the same
five-method surface the repositories fold over. Postgres (shared, multi-instance) is proven
against a real container in test_event_store_postgres.py (gated).
"""
import pytest

from agentic_os.mission.store import EventStore, MissionRepository
from agentic_os.mission.event_backends import (
    DuckDBEventStore, PostgresEventStore, build_durable_event_store, build_event_store, open_event_store,
)

duckdb = pytest.importorskip("duckdb")


def _conformance(store):
    seen = []
    store.subscribe(seen.append)                      # in-process fan-out
    e1 = store.append("A", "m1", {"n": 1, "nested": {"k": "v"}})
    e2 = store.append("B", "m1", {"n": 2})
    e3 = store.append("C", "m2", {"n": 3})

    assert e1.seq < e2.seq < e3.seq                   # monotonic, backend-assigned
    m1 = store.for_mission("m1")
    assert [e.type for e in m1] == ["A", "B"]         # per-mission, in order
    assert m1[0].payload == {"n": 1, "nested": {"k": "v"}}   # payload round-trips
    assert len(store.all()) == 3
    assert store.mission_ids() == ["m1", "m2"]        # first-seen order
    assert [e.type for e in seen] == ["A", "B", "C"]  # subscribers fired
    # a repository folds the surface unchanged
    assert len(MissionRepository(store).timeline("m1")) == 2


def test_memory_backend_conformance():
    _conformance(EventStore())


def test_jsonl_backend_conformance(tmp_path):
    _conformance(EventStore(path=str(tmp_path / "log.jsonl")))


def test_duckdb_file_backend_conformance(tmp_path):
    _conformance(DuckDBEventStore(str(tmp_path / "events.duckdb")))


def test_duckdb_memory_backend_conformance():
    _conformance(DuckDBEventStore(":memory:"))


def test_jsonl_survives_restart(tmp_path):
    path = str(tmp_path / "log.jsonl")
    s1 = EventStore(path=path)
    s1.append("A", "m1", {"n": 1})
    s2 = EventStore(path=path)                         # reopen the same file
    assert [e.type for e in s2.for_mission("m1")] == ["A"]


def test_duckdb_survives_restart(tmp_path):
    path = str(tmp_path / "events.duckdb")
    s1 = DuckDBEventStore(path)
    s1.append("A", "m1", {"n": 1})
    s1.close()
    s2 = DuckDBEventStore(path)                        # reopen the same file
    assert [e.type for e in s2.for_mission("m1")] == ["A"]
    assert s2.append("B", "m1", {}).seq == 2           # sequence continues, not reset


def test_selector_defaults_and_choices(tmp_path):
    assert isinstance(open_event_store(), EventStore)                 # default = memory
    assert isinstance(open_event_store("memory"), EventStore)
    assert isinstance(open_event_store("jsonl", path=str(tmp_path / "l.jsonl")), EventStore)
    assert isinstance(open_event_store("duckdb", path=":memory:"), DuckDBEventStore)


def test_selector_rejects_unknown_and_missing_dsn():
    with pytest.raises(ValueError):
        open_event_store("mystery")
    with pytest.raises(ValueError):
        open_event_store("postgres")                   # needs a DSN


# ── selection wiring: every assembler honours the durable backend, not just the factory ──

def test_build_event_store_default_unchanged(monkeypatch, tmp_path):
    monkeypatch.delenv("MISSION_EVENT_BACKEND", raising=False)
    monkeypatch.delenv("MISSION_EVENT_LOG", raising=False)
    assert type(build_event_store()) is EventStore                      # zero-dep default preserved
    assert type(build_event_store(str(tmp_path / "l.jsonl"))) is EventStore


def test_build_event_store_selects_duckdb(monkeypatch, tmp_path):
    monkeypatch.setenv("MISSION_EVENT_BACKEND", "duckdb")
    assert isinstance(build_event_store(str(tmp_path / "e.duckdb")), DuckDBEventStore)


def test_assemblers_honour_duckdb_backend(monkeypatch, tmp_path):
    # the gap this closes: service/production selected JSONL directly, ignoring MISSION_EVENT_BACKEND
    monkeypatch.setenv("MISSION_EVENT_BACKEND", "duckdb")
    monkeypatch.setenv("MISSION_EVENT_PATH", str(tmp_path / "svc.duckdb"))
    from agentic_os.mission.service import build_pilot_runtime
    rt = build_pilot_runtime()
    assert isinstance(rt.store, DuckDBEventStore)                       # durable ledger, from config alone


# ── production selection fails closed: no opportunistic default (reproducible deployments) ──

@pytest.mark.parametrize("bad", [None, "memory", "jsonl", "mystery"])
def test_build_durable_fails_closed(monkeypatch, bad):
    if bad is None:
        monkeypatch.delenv("MISSION_EVENT_BACKEND", raising=False)
    else:
        monkeypatch.setenv("MISSION_EVENT_BACKEND", bad)
    with pytest.raises(RuntimeError):                       # dev-only / unset / unknown → no silent default
        build_durable_event_store()


def test_build_durable_accepts_duckdb(monkeypatch, tmp_path):
    monkeypatch.setenv("MISSION_EVENT_BACKEND", "duckdb")
    assert isinstance(build_durable_event_store(str(tmp_path / "e.duckdb")), DuckDBEventStore)
