"""Inference receipts — prove what crossed the boundary (Private Data Plane plan §14).

EXPLAIN should answer which model saw which evidence, where it ran, which tools received it, and whether anything
crossed the trust boundary. The "records sent to external LLMs: 0" claim is backed by these receipts, not asserted.
Pure.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Tuple

from .classification import DataClassification, externally_shareable
from .routing import ExecutionBoundary, ModelEndpoint


@dataclass(frozen=True)
class InferenceReceipt:
    model: str
    provider: str
    execution_boundary: ExecutionBoundary
    maximum_classification: DataClassification
    deployment: str = ""
    network_route: str = ""
    external_data_processor: bool = False
    evidence_refs: Tuple[str, ...] = ()
    tool_calls: Tuple[str, ...] = ()
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    @property
    def crossed_boundary(self) -> bool:
        """True iff private data was handled by something outside the trust boundary (a violation if ever True)."""
        return (self.execution_boundary is ExecutionBoundary.EXTERNAL or self.external_data_processor) \
            and not externally_shareable(self.maximum_classification)


def receipt_for(endpoint: ModelEndpoint, *, maximum_classification: DataClassification,
                evidence_refs: Tuple[str, ...] = (), tool_calls: Tuple[str, ...] = ()) -> InferenceReceipt:
    return InferenceReceipt(
        model=endpoint.model_id, provider=endpoint.provider, execution_boundary=endpoint.boundary,
        maximum_classification=maximum_classification, deployment=endpoint.deployment,
        network_route=endpoint.network_route, external_data_processor=endpoint.external_data_processor,
        evidence_refs=tuple(evidence_refs), tool_calls=tuple(tool_calls))


def private_records_to_external(receipts) -> int:
    """The audited count behind 'records sent to external LLMs: 0' — receipts where private data left the boundary."""
    return sum(1 for r in receipts if r.crossed_boundary)


__all__ = ["InferenceReceipt", "receipt_for", "private_records_to_external"]
