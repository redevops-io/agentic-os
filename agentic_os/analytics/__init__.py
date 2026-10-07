"""Cross-app analytical dataset plane — materialize canonical observations into a queryable, provenance-tracked
dataset so BI is genuinely cross-app (CRM + support + billing + ERP), not a single-database read.

- ``contracts`` — DatasetDefinition / ColumnSpec / DatasetRefreshPolicy, SourceObservations, and the output
  AnalyticalRow + AnalyticalRowProvenance (bitemporal, entity-resolution aware) + MaterializedDataset.
- ``materialize`` — the entity-resolved cross-app join (reuses integration.identity.resolve; conflicts flagged,
  never merged) with per-row provenance + freshness.
- ``store`` — an in-memory dataset store with staleness against the refresh policy.

The enterprise overlay persists these as `analytics.*` Postgres relations + a Metabase datasource; this kernel
is deterministic and offline (fake-until-credentialed).
"""
from .contracts import (
    AnalyticalRow, AnalyticalRowProvenance, ColumnSpec, DatasetDefinition, DatasetMode, DatasetRefreshPolicy,
    Freshness, MaterializedDataset, SourceObservations, worst_freshness,
)
from .materialize import materialize
from .store import InMemoryDatasetStore

__all__ = [
    "DatasetMode", "Freshness", "worst_freshness", "ColumnSpec", "DatasetRefreshPolicy", "DatasetDefinition",
    "SourceObservations", "AnalyticalRowProvenance", "AnalyticalRow", "MaterializedDataset",
    "materialize", "InMemoryDatasetStore",
]
