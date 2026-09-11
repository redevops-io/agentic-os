"""Unit tests for connector→Observation adapters + the persisting ingestor (no DB)."""
from __future__ import annotations

from agentic_os.observation import KnownAtQuality
from agentic_os.observation_adapters import (
    build_persisting_ingestor, waha_message_observation)


def test_waha_inbound_message_maps_to_a_bitemporal_observation():
    ev = {"id": "m1", "body": "Oi, tudo bem?", "fromMe": False, "timestamp": 1789000000,
          "received_at": 1789000003, "chatId": "5511@c.us"}
    obs, deltas, changes = waha_message_observation(ev, now=1789000005)
    assert obs.observation_id == "m1" and obs.source == "whatsapp_waha"
    assert obs.kind == "chat.message.received" and obs.subject == "5511@c.us"
    assert obs.valid_at == 1789000000          # when the message existed in the world
    assert obs.known_at == 1789000003          # webhook receipt (a real source timestamp)
    assert obs.ingested_at == 1789000005       # our persist time (distinct)
    assert obs.known_at_quality is KnownAtQuality.OBSERVED
    assert len(deltas) == 1 and deltas[0].field == "last_inbound_at"
    assert len(changes) == 1 and changes[0].refs == ("m1",)


def test_waha_outbound_message_is_sent_kind_no_evidence_change():
    ev = {"id": "m2", "fromMe": True, "timestamp": 1789000060, "chatId": "5511@c.us"}
    obs, deltas, changes = waha_message_observation(ev)
    assert obs.kind == "chat.message.sent" and deltas[0].field == "last_outbound_at"
    assert changes == []                        # our own send isn't new inbound evidence


def test_known_at_defaults_to_timestamp_when_no_receipt():
    obs, _, _ = waha_message_observation({"id": "m3", "timestamp": 1789000100, "chatId": "x"})
    assert obs.known_at == 1789000100 and obs.known_at_quality is KnownAtQuality.OBSERVED


def test_persisting_ingestor_writes_every_observation_to_the_store_first():
    saved = []

    class _Store:
        def append(self, obs):
            saved.append(obs.observation_id)

    downstream = []
    ing = build_persisting_ingestor(_Store(), extra_sinks=[lambda o, d, c: downstream.append(o.kind)])
    ing.ingest("whatsapp_waha", {"id": "m9", "fromMe": False, "timestamp": 1789000200, "chatId": "y"})
    assert saved == ["m9"]                      # persisted
    assert downstream == ["chat.message.received"]   # and fanned out to the extra sink
