"""Integration tests for the Postgres observation store (PR-sequence.odt PR 1).

Skipped unless a Postgres is reachable via $OBS_DATABASE_URL (and psycopg is installed). When it runs,
it verifies the bi-temporal / as-of / A0-quality semantics end-to-end against a real database — the
'verify bi-temporal semantics end-to-end' acceptance for PR 1.
"""
from __future__ import annotations

import time
import uuid

import pytest

from agentic_os.observation import KnownAtQuality, Observation
from agentic_os.observation_adapters import build_persisting_ingestor

pytest.importorskip("psycopg")
from agentic_os.observation_store import PostgresObservationStore, observation_dsn  # noqa: E402


@pytest.fixture()
def store():
    if not observation_dsn():
        pytest.skip("set OBS_DATABASE_URL to run the Postgres observation-store integration tests")
    try:
        s = PostgresObservationStore()
    except Exception as e:                       # unreachable / auth / etc.
        pytest.skip(f"Postgres not reachable: {e}")
    yield s
    s.close()


def _obs(oid, subject, valid_at, known_at, quality=KnownAtQuality.OBSERVED, ingested_at=None):
    return Observation(observation_id=oid, source="whatsapp_waha", kind="chat.message.received",
                       subject=subject, valid_at=valid_at, known_at=known_at,
                       ingested_at=ingested_at if ingested_at is not None else known_at,
                       known_at_quality=quality, payload={"body": "hi"})


def test_append_get_roundtrip_preserves_all_three_timestamps(store):
    subj = f"acct-{uuid.uuid4().hex[:8]}"
    o = _obs(f"m-{uuid.uuid4().hex}", subj, valid_at=1000.0, known_at=1002.0, ingested_at=1004.0)
    store.append(o)
    back = store.get(o.observation_id)
    assert back is not None
    assert back.valid_at == 1000.0 and back.known_at == 1002.0 and back.ingested_at == 1004.0
    assert back.known_at_quality is KnownAtQuality.OBSERVED and back.payload == {"body": "hi"}


def test_append_is_idempotent_on_observation_id(store):
    subj = f"acct-{uuid.uuid4().hex[:8]}"
    oid = f"m-{uuid.uuid4().hex}"
    store.append(_obs(oid, subj, 1.0, 1.0))
    store.append(_obs(oid, subj, 1.0, 1.0))          # re-delivered webhook → no duplicate
    assert len(store.as_of(10.0, subject=subj)) == 1


def test_as_of_excludes_future_on_both_time_axes(store):
    subj = f"acct-{uuid.uuid4().hex[:8]}"
    store.append(_obs(f"past-{uuid.uuid4().hex}", subj, valid_at=10.0, known_at=10.0))
    store.append(_obs(f"validfut-{uuid.uuid4().hex}", subj, valid_at=200.0, known_at=50.0))   # true later
    store.append(_obs(f"knownfut-{uuid.uuid4().hex}", subj, valid_at=10.0, known_at=200.0))   # learned later
    kept = store.as_of(100.0, subject=subj)
    assert len(kept) == 1 and kept[0].observation_id.startswith("past-")


def test_as_of_fails_closed_on_unknown_provenance(store):
    subj = f"acct-{uuid.uuid4().hex[:8]}"
    store.append(_obs(f"ok-{uuid.uuid4().hex}", subj, 10.0, 10.0, quality=KnownAtQuality.OBSERVED))
    store.append(_obs(f"rec-{uuid.uuid4().hex}", subj, 10.0, 10.0, quality=KnownAtQuality.RECONSTRUCTED))
    store.append(_obs(f"unk-{uuid.uuid4().hex}", subj, 10.0, 10.0, quality=KnownAtQuality.UNKNOWN))
    assert len(store.as_of(100.0, subject=subj)) == 2                       # UNKNOWN excluded (A0)
    assert len(store.as_of(100.0, subject=subj, require_quality=False)) == 3  # exploratory includes it


def test_ingestor_persists_a_waha_event_end_to_end(store):
    subj = f"acct-{uuid.uuid4().hex[:8]}"
    ing = build_persisting_ingestor(store)
    mid = f"m-{uuid.uuid4().hex}"
    ing.ingest("whatsapp_waha", {"id": mid, "fromMe": False, "timestamp": 500.0,
                                 "received_at": 502.0, "chatId": subj})
    got = store.as_of(1000.0, subject=subj)
    assert [o.observation_id for o in got] == [mid]
    assert got[0].valid_at == 500.0 and got[0].known_at == 502.0
    assert got[0].known_at_quality is KnownAtQuality.OBSERVED
