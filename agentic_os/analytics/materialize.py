"""Materialize a cross-app dataset — the entity-resolved join with provenance.

Groups each app's observations by the join key, resolves the identity across apps with the open-core
entity-resolution plane (``integration.identity.resolve`` — which refuses to merge on conflict), projects the
dataset's columns, and attaches per-row provenance (sources, bitemporal timestamps, freshness, resolution status
+ confidence). Ambiguous/conflicted rows are kept but FLAGGED. Deterministic + offline.
"""
from __future__ import annotations

import time
from typing import Dict, List, Mapping, Sequence, Tuple

from ..integration.contracts import EntityType, ResolutionStatus
from ..integration.identity import Match, resolve
from .contracts import (
    AnalyticalRow, AnalyticalRowProvenance, DatasetDefinition, Freshness, MaterializedDataset, SourceObservations,
    worst_freshness,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _entity_type(name: str) -> EntityType:
    try:
        return EntityType(name)
    except ValueError:
        return EntityType.ACCOUNT          # a reasonable default for a business key


def _apply_sla(src: SourceObservations, now_ms: int, sla_s: int) -> Freshness:
    if src.freshness is not Freshness.FRESH or not sla_s or not src.observed_at:
        return src.freshness
    return Freshness.STALE if (now_ms - src.observed_at) > sla_s * 1000 else Freshness.FRESH


def materialize(defn: DatasetDefinition, sources: Sequence[SourceObservations], *,
                now_ms: int | None = None, transformation_version: str = "v1") -> MaterializedDataset:
    now = now_ms if now_ms is not None else _now_ms()
    etype = _entity_type(defn.entity_type)
    sla = defn.refresh.freshness_sla_s

    # index each source's rows by join key (a LIST per key, so a source with >1 row for one key surfaces as a
    # CONFLICTED identity rather than being silently deduped) + its SLA-adjusted freshness
    by_resource: Dict[str, Dict[str, List[Mapping]]] = {}
    src_freshness: Dict[str, Freshness] = {}
    for s in sources:
        idx: Dict[str, List[Mapping]] = {}
        for row in s.rows:
            k = str(row.get(s.key_field, "")).strip().lower()
            if k:
                idx.setdefault(k, []).append(row)
        by_resource[s.resource_id] = idx
        src_freshness[s.resource_id] = _apply_sla(s, now, sla)
    src_by_id = {s.resource_id: s for s in sources}

    all_keys = sorted({k for idx in by_resource.values() for k in idx})
    out_rows: List[AnalyticalRow] = []
    conflicts: List[str] = []

    for key in all_keys:
        present = [rid for rid in by_resource if key in by_resource[rid]]
        matches = [Match(resource_id=rid, external_id=str(r.get(src_by_id[rid].id_field, "")),
                         key_value=key, fields=dict(r))
                   for rid in present for r in by_resource[rid][key]]
        ent = resolve(etype, defn.key_field, key, matches)

        def _first(rid: str) -> Mapping:
            return by_resource.get(rid, {}).get(key, [{}])[0]

        values: Dict[str, object] = {}
        for col in defn.columns:
            if not col.source_resource:
                values[col.name] = _first(present[0]).get(src_by_id[present[0]].key_field, key) if present else key
            else:
                values[col.name] = _first(col.source_resource).get(col.source_field)

        contributing = [src_by_id[rid] for rid in present]
        freshness = worst_freshness([src_freshness[rid] for rid in present]) if present else Freshness.UNKNOWN
        observed_for = min((s.observed_at for s in contributing if s.observed_at), default=0)
        known_at = max((s.known_at for s in contributing if s.known_at), default=0)
        if ent.resolution_status in (ResolutionStatus.CONFLICTED, ResolutionStatus.AMBIGUOUS):
            conflicts.append(key)

        out_rows.append(AnalyticalRow(
            key=key, values=values,
            prov=AnalyticalRowProvenance(
                row_key=key, generated_at=now,
                source_resources=tuple(present),
                source_systems=tuple(src_by_id[rid].system or rid for rid in present),
                evidence_ids=tuple(ent.evidence),
                observed_for=observed_for, known_at=known_at, freshness=freshness,
                confidence=ent.confidence, entity_resolution=ent.resolution_status.value,
                transformation_version=transformation_version)))

    dataset_freshness = worst_freshness([r.prov.freshness for r in out_rows]) if out_rows else Freshness.UNKNOWN
    return MaterializedDataset(
        name=defn.name, mode=defn.refresh.mode, columns=tuple(c.name for c in defn.columns),
        rows=tuple(out_rows), generated_at=now, freshness=dataset_freshness,
        source_systems=tuple(sorted({s.system or s.resource_id for s in sources})),
        conflicts=tuple(conflicts))


__all__ = ["materialize"]
