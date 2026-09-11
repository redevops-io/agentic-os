"""Tests for the single observation ingestion path (agentic_os.observation)."""
from __future__ import annotations

import pytest

from agentic_os.observation import (
    EvidenceChange, Observation, ObservationIngestor, StateDelta)


def _crm_mapper(ev):
    o = Observation(observation_id=ev["id"], source="crm", kind="crm.reply", subject=ev["account"],
                    valid_at=ev["valid_at"], known_at=ev["known_at"], payload=ev)
    deltas = [StateDelta(entity=ev["account"], field="last_reply_at", old=None, new=ev["valid_at"],
                         valid_at=ev["valid_at"], known_at=ev["known_at"], source="crm")]
    changes = [EvidenceChange(entity=ev["account"], summary="customer replied",
                              valid_at=ev["valid_at"], known_at=ev["known_at"], source="crm")]
    return o, deltas, changes


def test_ingest_maps_once_and_fans_out_to_every_sink():
    seen = []
    ing = (ObservationIngestor()
           .register_mapper("crm", _crm_mapper)
           .add_sink(lambda o, d, c: seen.append(("discovery", o.kind)))
           .add_sink(lambda o, d, c: seen.append(("worldstate", len(d))))
           .add_sink(lambda o, d, c: seen.append(("attribution", o.subject))))
    obs, deltas, changes = ing.ingest("crm", {"id": "o1", "account": "Acme",
                                              "valid_at": 100.0, "known_at": 101.0})
    assert obs.kind == "crm.reply" and obs.subject == "Acme"
    assert obs.valid_at == 100.0 and obs.known_at == 101.0          # bi-temporal preserved
    assert len(deltas) == 1 and len(changes) == 1
    assert seen == [("discovery", "crm.reply"), ("worldstate", 1), ("attribution", "Acme")]


def test_one_bad_sink_does_not_block_the_others():
    seen = []

    def boom(o, d, c):
        raise RuntimeError("sink failed")

    ing = (ObservationIngestor().register_mapper("crm", _crm_mapper)
           .add_sink(boom).add_sink(lambda o, d, c: seen.append(o.observation_id)))
    ing.ingest("crm", {"id": "o2", "account": "Beta", "valid_at": 1.0, "known_at": 1.0})
    assert seen == ["o2"]                                            # the good sink still ran


def test_unregistered_source_raises():
    with pytest.raises(KeyError):
        ObservationIngestor().ingest("unknown", {})
