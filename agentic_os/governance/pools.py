"""Worker-pool separation — the deployment seam for §7 (private vs engineering execution).

Workers are assigned to pools by the DATA they touch, independent of mode: a worker handling private business data
belongs to the PRIVATE pool (no arbitrary external egress, private-data credentials); a worker handling only
ENGINEERING/PUBLIC context belongs to the ENGINEERING pool (external egress permitted, NO private-data credentials).
This module is the provider-neutral contract + assignment; the enterprise overlay enforces the actual network/
credential separation so a policy violation is hard even if application code is wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .classification import DataClassification, externally_shareable


class WorkerPoolKind(str, Enum):
    PRIVATE_WORKER_POOL = "private_worker_pool"          # no arbitrary egress; private model endpoints + creds
    ENGINEERING_WORKER_POOL = "engineering_worker_pool"  # approved external egress; no private-data creds


@dataclass(frozen=True)
class WorkerPool:
    kind: WorkerPoolKind
    allows_external_egress: bool
    private_credentials: bool
    max_classification: DataClassification


PRIVATE_POOL = WorkerPool(WorkerPoolKind.PRIVATE_WORKER_POOL, allows_external_egress=False,
                          private_credentials=True, max_classification=DataClassification.SECRET)
ENGINEERING_POOL = WorkerPool(WorkerPoolKind.ENGINEERING_WORKER_POOL, allows_external_egress=True,
                              private_credentials=False, max_classification=DataClassification.ENGINEERING)


def pool_for(classification: DataClassification) -> WorkerPoolKind:
    """A worker touching data above ENGINEERING must run in the PRIVATE pool; otherwise the ENGINEERING pool."""
    return (WorkerPoolKind.ENGINEERING_WORKER_POOL if externally_shareable(classification)
            else WorkerPoolKind.PRIVATE_WORKER_POOL)


def worker_may_handle(pool: WorkerPool, classification: DataClassification) -> bool:
    """A pool may handle data only up to its max classification (engineering pool can't touch private business data)."""
    return classification.rank <= pool.max_classification.rank


__all__ = ["WorkerPoolKind", "WorkerPool", "PRIVATE_POOL", "ENGINEERING_POOL", "pool_for", "worker_may_handle"]
