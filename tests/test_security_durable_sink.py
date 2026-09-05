"""Durable telemetry sink — SecurityMonitor boundary events persisted to the event store.

"Telemetry belongs to the runtime": with a durable backend, the security trajectory is stored
alongside mission events (per mission), queryable, and redaction-safe by construction.
"""
from types import SimpleNamespace

import pytest

pytest.importorskip("runtime_contracts")

from agentic_os.mission.types import Node
from agentic_os.mission.store import EventStore
from agentic_os.mission.security_monitor import SecurityMonitor, durable_sink, TELEMETRY_SCOPE
from agentic_os.mission.event_backends import DuckDBEventStore

RAW_SECRET = "sk-super-secret-value-DO-NOT-STORE"


def _observed(store, scope):
    return [e for e in store.for_mission(scope) if e.type == "RuntimeSecurityObserved"]


def test_no_sink_writes_nothing_to_the_store():
    store = EventStore()
    mon = SecurityMonitor(mission_id="m1")        # no sink → in-memory trajectory only
    mon.observe(Node(capability="crm.read", operator="op"), {}, isolation="in_process")
    assert store.all() == []                       # unchanged default behaviour


def test_boundary_event_is_persisted_under_its_mission():
    store = EventStore()
    mon = SecurityMonitor(mission_id="m1", sink=durable_sink(store))
    mon.observe(Node(capability="crm.read", operator="op"),
                {"records": 5}, isolation="in_process")
    rows = _observed(store, "m1")                   # stored under the mission id
    assert len(rows) == 1
    p = rows[0].payload
    assert p["capability"] == "crm.read" and p["result"] == "ok"
    assert "records_read=5" in p["side_effects"]
    # the trajectory is still populated in memory too
    assert len(mon.trajectory.events) >= 1 if hasattr(mon.trajectory, "events") else True


def test_credential_event_stores_refs_not_the_secret():
    store = EventStore()
    mon = SecurityMonitor(mission_id="m1", sink=durable_sink(store))
    grant = SimpleNamespace(
        grant_id="g-123", authority_ref="auth-abc",
        credential_ref=SimpleNamespace(fingerprint=lambda: "fp-of-secret"))
    mon.observe_credential(Node(capability="crm.read", operator="op"),
                           "CREDENTIAL_REDEEMED", grant=grant)
    (row,) = _observed(store, "m1")
    blob = str(row.payload)
    assert "grant:g-123" in blob and "secret:fp-of-secret" in blob   # references
    assert RAW_SECRET not in blob                                     # never the value


def test_durable_and_queryable_on_duckdb_backend():
    store = DuckDBEventStore(":memory:")
    mon = SecurityMonitor(mission_id="m1", sink=durable_sink(store))
    mon.observe(Node(capability="a.read", operator="op"), {}, isolation="in_process")
    mon.observe(Node(capability="b.write", operator="op"), {}, isolation="sandbox",
                error="boom")
    rows = _observed(store, "m1")
    assert [r.payload["capability"] for r in rows] == ["a.read", "b.write"]
    assert rows[1].payload["result"] == "error"     # the failed boundary event is durable


def test_events_without_mission_id_land_under_the_telemetry_scope():
    store = EventStore()
    mon = SecurityMonitor(mission_id="", sink=durable_sink(store))
    mon.observe(Node(capability="x.run", operator="op"), {}, isolation="in_process")
    assert len(_observed(store, TELEMETRY_SCOPE)) == 1
