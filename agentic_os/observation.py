"""Observation ingestion — the ONE path from real-world connector events into the stack
(REAL_DATA_PROVIDER_SCOPING.md v2 §2/§3.2).

A connector event ("customer replied", "PR merged", "metric moved") is rarely just one thing. It can
simultaneously be an `EvidenceChange` (new input for Discovery), a `StateDelta` (an entity's tracked
state changed), a Mission observation, a WorldState update, and — only when it correlates with a prior
intervention — an outcome. So real signals do NOT get a bespoke "outcome collector"; they enter through
a single `ObservationIngestor` that produces canonical, **bi-temporal** records and fans them out to
whatever sinks care (Discovery, Mission, WorldState projection, outcome attribution).

Bi-temporality is the load-bearing property (it makes leakage-proof historical replay possible):

    valid_at  — when the fact became TRUE in the world
    known_at  — when WE learned it (ingestion / observation time)

Nothing here generates intelligence or decides anything — it is pure perception plumbing. Deterministic
and dependency-free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Tuple


class KnownAtQuality(Enum):
    """How defensible an observation's ``known_at`` is — the guard against leakage-through-inference.
    A0 retrospective evaluation may only use OBSERVED or RECONSTRUCTED; UNKNOWN fails closed, so nobody
    can weaken the leakage gate later just to get more training data."""
    OBSERVED = "observed"            # an exact source timestamp (webhook/delivery/received/created_at)
    RECONSTRUCTED = "reconstructed"  # derived from immutable event/audit history (record the provenance)
    UNKNOWN = "unknown"              # only valid_at / a current snapshot — exploratory ONLY, never A0


@dataclass(frozen=True)
class Observation:
    """A canonical, bi-temporal observation of the world, produced from a connector event.

    Three timestamps, three different meanings (all preserved):
      ``valid_at``     — when the fact became true in the world
      ``known_at``     — when the observing system could legitimately have known it (replay correctness)
      ``ingested_at``  — when THIS deployment persisted it (operational provenance: latency/outages)
    They differ: a reply at 10:01, a provider webhook at 10:02, our receipt at 10:04."""
    observation_id: str
    source: str                              # connector / app that produced it (e.g. "crm", "email")
    kind: str                                # e.g. "crm.reply", "pr.merged", "metric.moved"
    subject: str                             # the entity the observation is about
    valid_at: float                          # when the fact became true in the world
    known_at: float                          # when it could legitimately be known (drives replay safety)
    payload: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: Tuple[str, ...] = ()
    ingested_at: float = 0.0                 # when this deployment persisted it (operational only)
    known_at_quality: KnownAtQuality = KnownAtQuality.UNKNOWN  # provenance of known_at (A0 admissibility)


@dataclass(frozen=True)
class StateDelta:
    """A change to an entity's tracked state — feeds WorldState projection + Mission observation."""
    entity: str
    field: str
    old: Any
    new: Any
    valid_at: float
    known_at: float
    source: str = ""


@dataclass(frozen=True)
class EvidenceChange:
    """New or changed evidence about an entity — feeds Discovery's incremental interpretation."""
    entity: str
    summary: str
    valid_at: float
    known_at: float
    refs: Tuple[str, ...] = ()
    source: str = ""


# A mapper turns a raw connector event into the canonical trio. A sink consumes it.
Mapper = Callable[[Dict[str, Any]], Tuple[Observation, List[StateDelta], List[EvidenceChange]]]
Sink = Callable[[Observation, List[StateDelta], List[EvidenceChange]], None]


@dataclass
class ObservationIngestor:
    """The single ingestion path. Register a `mapper` per source (raw event → canonical records) and any
    number of `sinks` (Discovery, Mission observation, WorldState, outcome attribution). ``ingest``
    maps once and fans out — so a connector event reaches every interested consumer through one path,
    not a second bespoke pipeline per concern."""
    mappers: Dict[str, Mapper] = field(default_factory=dict)
    sinks: List[Sink] = field(default_factory=list)

    def register_mapper(self, source: str, mapper: Mapper) -> "ObservationIngestor":
        self.mappers[source] = mapper
        return self

    def add_sink(self, sink: Sink) -> "ObservationIngestor":
        self.sinks.append(sink)
        return self

    def ingest(self, source: str, raw_event: Dict[str, Any]
               ) -> Tuple[Observation, List[StateDelta], List[EvidenceChange]]:
        """Map a raw connector event to canonical records and fan out to every sink. Returns the trio.
        A sink that raises does not stop the others (perception must be robust to one consumer failing)."""
        mapper = self.mappers.get(source)
        if mapper is None:
            raise KeyError(f"no observation mapper registered for source {source!r}")
        obs, deltas, changes = mapper(raw_event)
        for sink in self.sinks:
            try:
                sink(obs, deltas, changes)
            except Exception:
                continue                     # one bad sink never blocks perception of the rest
        return obs, deltas, changes
