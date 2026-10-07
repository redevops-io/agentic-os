"""Cross-app analytical dataset plane — contracts (plan §8/§9/§21, matrix row 6).

Metabase expects queryable relations, but agentic-apps data lives across SaaS APIs. This plane materializes
canonical observations from several apps, joined on a RESOLVED entity, into one analytical dataset — so Metabase
queries a stable relation instead of orchestrating live SaaS calls. Every row carries bitemporal provenance
(which systems, when observed/known, how fresh, which identity match produced it), and ambiguous/conflicted
identities are FLAGGED, never silently aggregated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Tuple


class DatasetMode(str, Enum):
    LIVE = "live"                    # query sources live (no materialization)
    SNAPSHOT = "snapshot"            # a frozen point-in-time materialization
    REFRESHED = "refreshed"          # periodically re-materialized
    MISSION_OUTPUT = "mission_output"  # produced by a governed Mission
    EVIDENCE_VIEW = "evidence_view"  # a read-only projection of evidence


class Freshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    FAILED = "failed"
    UNKNOWN = "unknown"


_FRESH_RANK = {Freshness.FRESH: 0, Freshness.UNKNOWN: 1, Freshness.STALE: 2, Freshness.FAILED: 3}


def worst_freshness(values) -> Freshness:
    """The least-fresh of a set — a dataset is only as fresh as its stalest contributing source."""
    out = Freshness.FRESH
    for f in values:
        if _FRESH_RANK[f] > _FRESH_RANK[out]:
            out = f
    return out


@dataclass(frozen=True)
class ColumnSpec:
    """One analytical column. ``source_resource`` empty → the identity/key column; otherwise the column is pulled
    from that app's observation. ``metric_id`` records the Semantic Registry meaning (which 'revenue')."""
    name: str
    source_resource: str = ""
    source_field: str = ""
    metric_id: str = ""


@dataclass(frozen=True)
class DatasetRefreshPolicy:
    mode: DatasetMode = DatasetMode.SNAPSHOT
    interval_s: int = 0
    freshness_sla_s: int = 0             # 0 = no SLA; else an observation older than this is STALE
    dependencies: Tuple[str, ...] = ()
    failure_policy: str = "surface"      # surface | last_good


@dataclass(frozen=True)
class DatasetDefinition:
    """A cross-app analytical dataset: key rows on a resolved ``entity_type`` (via ``key_field``), project the
    columns from each source app."""
    name: str
    entity_type: str                     # canonical entity (an integration EntityType value), e.g. "account"
    key_field: str                       # the strong key sources are joined on, e.g. "email"
    columns: Tuple[ColumnSpec, ...]
    refresh: DatasetRefreshPolicy = field(default_factory=DatasetRefreshPolicy)


@dataclass(frozen=True)
class SourceObservations:
    """Normalized rows from ONE app, each carrying the join key + an external id."""
    resource_id: str                     # e.g. "salesforce" | "chatwoot" | "lago" | "erpnext"
    rows: Tuple[Mapping[str, Any], ...]
    key_field: str
    id_field: str = "id"
    system: str = ""                     # provenance label (defaults to resource_id)
    observed_at: int = 0
    known_at: int = 0
    freshness: Freshness = Freshness.FRESH


@dataclass(frozen=True)
class AnalyticalRowProvenance:
    """Why this analytical row says what it says (§9) — bitemporal + entity-resolution aware."""
    row_key: str
    generated_at: int
    source_resources: Tuple[str, ...] = ()
    source_systems: Tuple[str, ...] = ()
    evidence_ids: Tuple[str, ...] = ()
    observed_for: int = 0                # earliest source observed_at that fed this row
    known_at: int = 0
    freshness: Freshness = Freshness.UNKNOWN
    confidence: float = 0.0
    entity_resolution: str = "unresolved"   # a ResolutionStatus value
    transformation_version: str = "v1"


@dataclass(frozen=True)
class AnalyticalRow:
    key: str
    values: Mapping[str, Any]
    prov: AnalyticalRowProvenance


@dataclass(frozen=True)
class MaterializedDataset:
    name: str
    mode: DatasetMode
    columns: Tuple[str, ...]
    rows: Tuple[AnalyticalRow, ...]
    generated_at: int
    freshness: Freshness
    source_systems: Tuple[str, ...] = ()
    conflicts: Tuple[str, ...] = ()      # row keys whose identity was ambiguous/conflicted (flagged, not merged)

    def to_records(self) -> List[Dict[str, Any]]:
        """Flat rows (for a Metabase/SQL relation), each with a reserved ``_resolution`` + ``_freshness`` column
        so downstream never loses the provenance caveat."""
        return [{**r.values, "_resolution": r.prov.entity_resolution, "_freshness": r.prov.freshness.value}
                for r in self.rows]


__all__ = [
    "DatasetMode", "Freshness", "worst_freshness", "ColumnSpec", "DatasetRefreshPolicy", "DatasetDefinition",
    "SourceObservations", "AnalyticalRowProvenance", "AnalyticalRow", "MaterializedDataset",
]
