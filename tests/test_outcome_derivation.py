"""Tests for outcome derivation (agentic_os.outcome_derivation) — correlate → derive → persist,
no learning. Unit tests use in-memory stores; the PG block runs end-to-end when $OBS_DATABASE_URL is
set."""
from __future__ import annotations

import uuid

import pytest

from agentic_os.intervention_record import InMemoryInterventionStore, InterventionRecord
from agentic_os.observation import KnownAtQuality, Observation
from agentic_os.outcome_derivation import (
    derive_and_persist, outcome_derivation_sink, outreach_reward_map)
from agentic_os.outcome_store import InMemoryOutcomeStore


def _intervention(iid, subject, executed_at, action="contact"):
    return InterventionRecord(
        intervention_id=iid, opportunity_id=f"outreach:{subject}", candidate_id=f"outreach:{subject}:{action}",
        selected_action=action, alternatives=(), evidence_refs=(), policy_version="p1", score=0.8,
        proposed_at=executed_at, executed_at=executed_at)


def _obs(oid, subject, valid_at, outcome_label):
    return Observation(observation_id=oid, source="whatsapp_waha", kind="chat.message.received",
                       subject=subject, valid_at=valid_at, known_at=valid_at, ingested_at=valid_at,
                       known_at_quality=KnownAtQuality.OBSERVED, payload={"outcome": outcome_label})


def test_reward_map_reads_the_outcome_label():
    assert outreach_reward_map(_obs("o", "s", 1, "positive_reply")) == {"positive_reply": 1.0}
    assert outreach_reward_map(_obs("o", "s", 1, "unsubscribe")) == {"unsubscribe": -1.0}
    assert outreach_reward_map(_obs("o", "s", 1, "")) == {}          # unlabelled ⇒ no reward


def test_correlated_observation_derives_and_persists_outcome_and_link():
    ivs = InMemoryInterventionStore(); outs = InMemoryOutcomeStore()
    ivs.append(_intervention("iv1", "Acme", executed_at=0.0))
    res = derive_and_persist(_obs("oc1", "Acme", valid_at=3600.0, outcome_label="positive_reply"),
                             intervention_store=ivs, outcome_store=outs)
    assert res is not None
    ev, link = res
    assert outs.load() == [ev] and ev.reward_dimensions == {"positive_reply": 1.0}
    assert ev.selected_intervention_id == "iv1" and ev.source_observation_ids == ("oc1",)
    assert ivs.outcome_refs("iv1") == ["oc1"]                        # immutable link on the intervention
    assert link.attribution_confidence == ev.attribution_confidence


def test_uncorrelated_observation_yields_no_outcome():
    ivs = InMemoryInterventionStore(); outs = InMemoryOutcomeStore()
    # an intervention for a DIFFERENT subject → the reply can't be attributed to it
    ivs.append(_intervention("iv1", "Beta", executed_at=0.0))
    res = derive_and_persist(_obs("oc1", "Acme", 3600.0, "positive_reply"),
                             intervention_store=ivs, outcome_store=outs)
    assert res is None and outs.load() == [] and ivs.outcome_refs("iv1") == []


def test_unlabelled_observation_is_not_an_outcome():
    ivs = InMemoryInterventionStore(); outs = InMemoryOutcomeStore()
    ivs.append(_intervention("iv1", "Acme", executed_at=0.0))
    assert derive_and_persist(_obs("oc1", "Acme", 3600.0, ""), intervention_store=ivs,
                              outcome_store=outs) is None


def test_derivation_sink_closes_the_loop_on_ingest():
    from agentic_os.observation_adapters import build_persisting_ingestor
    ivs = InMemoryInterventionStore(); outs = InMemoryOutcomeStore()
    ivs.append(_intervention("iv1", "5511@c.us", executed_at=100.0))

    class _ObsStore:
        saved = []
        def append(self, o): self.saved.append(o)

    ing = build_persisting_ingestor(_ObsStore(), extra_sinks=[outcome_derivation_sink(ivs, outs)])
    # an inbound WAHA reply labelled positive → observation persisted AND an outcome derived
    ing.ingest("whatsapp_waha", {"id": "m1", "fromMe": False, "timestamp": 4000.0,
                                 "chatId": "5511@c.us", "outcome": "positive_reply"})
    assert len(outs.load()) == 1 and outs.load()[0].selected_intervention_id == "iv1"


# ── PG end-to-end (skipped without a database) ───────────────────────────────────────
pytest.importorskip("psycopg")
from agentic_os.outcome_store import PostgresOutcomeStore  # noqa: E402
from agentic_os.observation_store import observation_dsn  # noqa: E402
from agentic_os.intervention_record import PostgresInterventionStore  # noqa: E402


@pytest.fixture()
def pg_stores():
    if not observation_dsn():
        pytest.skip("set OBS_DATABASE_URL for the PG outcome-derivation end-to-end test")
    try:
        ivs = PostgresInterventionStore(); outs = PostgresOutcomeStore()
    except Exception as e:
        pytest.skip(f"Postgres not reachable: {e}")
    yield ivs, outs
    ivs.close(); outs.close()


def test_pg_end_to_end_reply_becomes_attributed_outcome(pg_stores):
    ivs, outs = pg_stores
    subject = f"acct-{uuid.uuid4().hex[:8]}"
    iid = f"iv-{uuid.uuid4().hex}"
    ivs.append(_intervention(iid, subject, executed_at=100.0))
    oid = f"oc-{uuid.uuid4().hex}"
    res = derive_and_persist(_obs(oid, subject, valid_at=100.0 + 3600, outcome_label="meeting"),
                             intervention_store=ivs, outcome_store=outs)
    assert res is not None
    # the immutable link lands on the intervention, and the derived outcome is durable in Postgres
    assert ivs.outcome_refs(iid) == [oid]
    persisted = [e for e in outs.load() if e.selected_intervention_id == iid]
    assert persisted and persisted[0].reward_dimensions == {"meeting": 1.0}
    assert persisted[0].source_observation_ids == (oid,)
