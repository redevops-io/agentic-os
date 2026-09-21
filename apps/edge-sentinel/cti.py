"""CTI graph + ATT&CK mapping + internal-asset correlation + a TAXII read seam (Phase 2).

The Phase 2 acceptance path lives here: seed an IOC → an evidence-linked STIX graph → ATT&CK mapping →
internal-impact result, with every derived edge carrying provenance. The graph is deterministic and
replayable (STIX ids are content-derived), and it hangs off the Phase 1 evidence spine — every derived
edge's ``provenance.evidence_refs`` point back at immutable evidence.

Nothing here calls a live TAXII server or OpenCTI: :class:`TaxiiSource` is the read seam, and
:class:`OfflineTaxiiSource` serves fixture bundles, so the whole thing is pure and testable. A real TAXII
2.1 client (or an OpenCTI connector) implements the same ``poll()`` and feeds :func:`ingest_bundle`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .attack import attack_pattern, map_scenario_to_techniques
from .stix import Provenance, Relationship, StixObject, normalize_ioc


@dataclass
class CtiGraph:
    """STIX objects (nodes) + provenanced relationships (edges). Adding the same object/edge twice is a
    no-op (content-addressed ids), so ingestion is idempotent and the graph is replayable."""
    objects: dict = field(default_factory=dict)      # stix id -> StixObject
    relationships: dict = field(default_factory=dict)  # rel id -> Relationship

    def add_object(self, obj: StixObject) -> StixObject:
        self.objects.setdefault(obj.id, obj)
        return self.objects[obj.id]

    def relate(self, relationship_type: str, source_ref: str, target_ref: str,
               provenance: Provenance, confidence: int = 50) -> Relationship:
        rel = Relationship(relationship_type, source_ref, target_ref, provenance, confidence)
        self.relationships.setdefault(rel.id, rel)
        return self.relationships[rel.id]

    def objects_of_type(self, stix_type: str) -> list[StixObject]:
        return [o for o in self.objects.values() if o.type == stix_type]

    def neighbors(self, stix_id: str) -> list[Relationship]:
        return [r for r in self.relationships.values()
                if r.source_ref == stix_id or r.target_ref == stix_id]

    def to_dict(self) -> dict:
        return {"type": "bundle",
                "objects": [o.to_dict() for o in self.objects.values()]
                           + [r.to_dict() for r in self.relationships.values()]}


# ──────────────────────────── TAXII read seam ────────────────────────────

class TaxiiSource(ABC):
    """A TAXII 2.1 read connector. A real client implements poll(); tests use OfflineTaxiiSource."""
    @abstractmethod
    def poll(self) -> list[dict]:
        """Return STIX bundle dicts."""


@dataclass
class OfflineTaxiiSource(TaxiiSource):
    bundles: list[dict]
    name: str = "offline-taxii"

    def poll(self) -> list[dict]:
        return list(self.bundles)


def ingest_bundle(graph: CtiGraph, bundle: dict, *, source: str) -> int:
    """Ingest a STIX bundle's SDO/SCO objects (indicators, attack-patterns, observables) into the graph.
    Relationships in a bundle are re-created with a TAXII-ingest provenance so their origin is explicit."""
    n = 0
    prov = Provenance(source=source, method="taxii-ingest")
    for o in bundle.get("objects", []):
        t = o.get("type")
        if t == "relationship":
            graph.relate(o.get("relationship_type", "related-to"), o["source_ref"], o["target_ref"],
                         prov, o.get("confidence", 50)); n += 1
        elif t:
            key = o.get("x_ioc_value") or o.get("external_references", [{}])[0].get("external_id") \
                  or o.get("value") or o.get("id", "")
            graph.add_object(StixObject(type=t, key=str(key),
                                        props={k: v for k, v in o.items() if k not in ("type", "id")},
                                        provenance=prov)); n += 1
    return n


# ──────────────────────────── the acceptance path ────────────────────────────

def seed_ioc(graph: CtiGraph, value: str, *, scenario: str = "", source: str = "analyst",
             evidence_refs: tuple[str, ...] = ()) -> StixObject:
    """Seed an IOC into the graph: normalize → add SCO + Indicator (+ an 'indicator-observes' edge), and,
    if a scenario is supplied, map ATT&CK techniques and add 'indicates' edges. Returns the Indicator.
    Every edge is provenanced with the supplied evidence_refs."""
    sco, indicator = normalize_ioc(value, source=source, evidence_refs=evidence_refs)
    graph.add_object(sco)
    graph.add_object(indicator)
    prov = Provenance(source=source, method="ioc-seed", evidence_refs=tuple(evidence_refs))
    graph.relate("based-on", indicator.id, sco.id, prov, confidence=90)
    for technique_id, token in map_scenario_to_techniques(scenario):
        ap = graph.add_object(attack_pattern(technique_id, evidence_refs=evidence_refs))
        graph.relate("indicates", indicator.id, ap.id,
                     Provenance(source=source, method=f"attack-map:{token}", evidence_refs=tuple(evidence_refs)),
                     confidence=70)
    return indicator


@dataclass(frozen=True)
class InternalImpact:
    indicator_id: str
    ioc_value: str
    observation_id: str
    matched_ref: str
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict:
        d = self.__dict__.copy(); d["evidence_refs"] = list(self.evidence_refs); return d


def correlate_internal(graph: CtiGraph, observations) -> list[InternalImpact]:
    """Correlate graph indicators against internal SecurityObservations (Phase 1). When an indicator's IOC
    value matches an observation's network/artifact ref, record a Sighting edge (indicator sighted-in the
    observation) with the observation's evidence as provenance, and return an InternalImpact. This is the
    'internal impact result, all derived edges with provenance' half of the acceptance."""
    # index observations by the bare values they reference (strip the 'ip:'/'sha256:' prefix)
    ref_index: dict[str, list] = {}
    for obs in observations:
        for ref in (*obs.network_refs, *obs.artifact_refs):
            bare = ref.split(":", 1)[1] if ":" in ref else ref
            ref_index.setdefault(bare.lower(), []).append((ref, obs))

    impacts: list[InternalImpact] = []
    for ind in graph.objects_of_type("indicator"):
        val = str(ind.props.get("x_ioc_value", "")).lower()
        for matched_ref, obs in ref_index.get(val, []):
            ev = (obs.raw_evidence_ref,) if obs.raw_evidence_ref else ()
            graph.relate("sighted-in", ind.id, f"observation--{obs.id}",
                         Provenance(source="edge-sentinel", method="sighting-correlation", evidence_refs=ev),
                         confidence=90)
            impacts.append(InternalImpact(indicator_id=ind.id, ioc_value=val,
                                          observation_id=obs.id, matched_ref=matched_ref, evidence_refs=ev))
    return impacts
