"""Compliance evidence producers (AGPL base).

Compliance is a *projection over the Runtime's existing truth*, not a second source of truth. The
Runtime already records what happened — the mission event ledger, human approvals, governance
grants, evidence lineage, verification outcomes, the capability inventory. This subpackage projects
those durable streams into the canonical :class:`runtime_contracts.ControlEvidence` shape, each row
tagged on ``control_id`` with the :class:`runtime_contracts.RuntimeStream` it came from.

The base stops here: it emits *stream-keyed* evidence about what the runtime did. Mapping a stream
onto a framework's controls (EU AI Act / NIST AI RMF / ISO 42001 / …) and assessing posture is the
Enterprise edition's job — it consumes exactly these rows.
"""
from __future__ import annotations

from .evidence_producers import (
    RuntimeEvidenceProducer,
    mission_evidence,
    system_evidence,
)

__all__ = [
    "RuntimeEvidenceProducer",
    "mission_evidence",
    "system_evidence",
]
