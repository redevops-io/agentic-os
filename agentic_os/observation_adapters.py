"""Connector → Observation adapters, and a persisting ingestor (PR-sequence.odt PR 1).

The first real connector into the single :class:`~agentic_os.observation.ObservationIngestor` path.
An adapter turns one connector's event shape into a canonical, bi-temporal `Observation` (+ the
`StateDelta`/`EvidenceChange` fan-out). `build_persisting_ingestor` wires an ingestor whose sink writes
every observation to the operational store (Postgres).

WhatsApp (WAHA) is the first: an inbound message ("customer replied") is a clean outreach/CRM signal
with an exact source timestamp, so its `known_at_quality` is OBSERVED — precisely the kind of
defensible provenance A0 requires. A raw message is ``{"id","body","fromMe","timestamp", …}`` with
``timestamp`` in unix seconds (when the message existed in the world).
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from agentic_os.observation import (
    EvidenceChange, KnownAtQuality, Observation, ObservationIngestor, StateDelta)


def waha_message_observation(event: Dict[str, Any], *, now: Optional[float] = None
                             ) -> Tuple[Observation, List[StateDelta], List[EvidenceChange]]:
    """Map a WAHA chat-message event to the canonical trio.

    ``valid_at`` = the message's own ``timestamp`` (when it existed in the world). ``known_at`` = the
    webhook receipt time if the event carries one (``received_at``), else the message timestamp — either
    way a real source timestamp, so provenance is OBSERVED. ``ingested_at`` = now (this deployment's
    persist time). ``fromMe`` distinguishes our own send from an inbound reply."""
    ts = float(event["timestamp"])
    known_at = float(event.get("received_at", ts))
    ingested = float(now if now is not None else time.time())
    inbound = not event.get("fromMe", False)
    subject = str(event.get("chatId") or event.get("from") or event.get("to") or "unknown")
    obs = Observation(
        observation_id=str(event["id"]), source="whatsapp_waha",
        kind="chat.message.received" if inbound else "chat.message.sent",
        subject=subject, valid_at=ts, known_at=known_at, ingested_at=ingested,
        known_at_quality=KnownAtQuality.OBSERVED, payload=dict(event))
    deltas = [StateDelta(entity=subject, field="last_inbound_at" if inbound else "last_outbound_at",
                         old=None, new=ts, valid_at=ts, known_at=known_at, source="whatsapp_waha")]
    changes = ([EvidenceChange(entity=subject, summary="inbound WhatsApp message", valid_at=ts,
                               known_at=known_at, refs=(str(event["id"]),), source="whatsapp_waha")]
               if inbound else [])
    return obs, deltas, changes


#: adapters by connector source id — extend as connectors are wired.
ADAPTERS: Dict[str, Callable[..., Tuple[Observation, List[StateDelta], List[EvidenceChange]]]] = {
    "whatsapp_waha": waha_message_observation,
}


def build_persisting_ingestor(store, *, adapters=None, extra_sinks=None) -> ObservationIngestor:
    """An ObservationIngestor whose FIRST sink persists every observation to the operational store,
    with any ``extra_sinks`` (Discovery, Mission observation, …) after it. ``store`` is anything with
    ``append(Observation)`` — e.g. a PostgresObservationStore."""
    ing = ObservationIngestor()
    for source, mapper in (adapters or ADAPTERS).items():
        ing.register_mapper(source, mapper)
    ing.add_sink(lambda obs, deltas, changes: store.append(obs))
    for sink in (extra_sinks or []):
        ing.add_sink(sink)
    return ing
