"""In-memory dataset store — the reference analytical plane.

Holds materialized datasets by name and answers freshness/staleness against each definition's refresh policy. An
enterprise overlay persists these as real `analytics.*` Postgres relations and registers them as a Metabase
datasource; this kernel keeps the materialization + provenance deterministic and offline.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .contracts import DatasetDefinition, Freshness, MaterializedDataset


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class InMemoryDatasetStore:
    _datasets: Dict[str, MaterializedDataset] = field(default_factory=dict)
    _defs: Dict[str, DatasetDefinition] = field(default_factory=dict)

    def put(self, dataset: MaterializedDataset, *, definition: Optional[DatasetDefinition] = None) -> None:
        self._datasets[dataset.name] = dataset
        if definition is not None:
            self._defs[dataset.name] = definition

    def get(self, name: str) -> Optional[MaterializedDataset]:
        return self._datasets.get(name)

    def list(self) -> List[str]:
        return sorted(self._datasets)

    def is_stale(self, name: str, *, now_ms: Optional[int] = None) -> bool:
        """True if the dataset's own freshness is not FRESH, or its interval/SLA has elapsed since it was built."""
        ds = self._datasets.get(name)
        if ds is None:
            return True
        if ds.freshness is not Freshness.FRESH:
            return True
        defn = self._defs.get(name)
        window_s = max(defn.refresh.interval_s, defn.refresh.freshness_sla_s) if defn else 0
        if not window_s:
            return False
        now = now_ms if now_ms is not None else _now_ms()
        return (now - ds.generated_at) > window_s * 1000


__all__ = ["InMemoryDatasetStore"]
