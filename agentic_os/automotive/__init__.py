"""Automotive car-trouble diagnosis — domain contracts + vehicle identity (P0).

The domain layer for the WhatsApp multimodal diagnosis service (see
``~/Documents/redevops_whatsapp_car_diagnosis_plan.md``). Contracts model the evidence→diagnosis
graph the product's moat is built on; ``decode_vin`` establishes vehicle identity from NHTSA vPIC so
everything downstream is model-specific. The diagnosis itself runs as a governed Mission over these
types, front-ended by the interaction operator + a WhatsApp channel adapter; older/less-used rows
tier from Apache Doris to an S3 datalake.
"""
from __future__ import annotations

from .contracts import (
    Diagnosis,
    DiagnosticEvidenceRequest,
    DiagnosticObservation,
    EvidenceType,
    Hypothesis,
    Powertrain,
    RepairEvidence,
    SafetyEvidence,
    Severity,
    SymptomEvidence,
    Urgency,
    VehicleRef,
)
from .vin import decode_vin
from .store_doris import DorisCaseStore
from .planner import (
    SafetyAssessment,
    plan_next_evidence,
    rank_evidence,
    safety_gate,
    should_stop_collecting,
    is_confident,
)
from .diagnose import Diagnoser, format_reply
from .service import DiagnosisService

__all__ = [
    "DorisCaseStore",
    "SafetyAssessment",
    "plan_next_evidence",
    "rank_evidence",
    "safety_gate",
    "should_stop_collecting",
    "is_confident",
    "Diagnoser",
    "format_reply",
    "DiagnosisService",
    "VehicleRef",
    "Powertrain",
    "DiagnosticObservation",
    "SymptomEvidence",
    "SafetyEvidence",
    "RepairEvidence",
    "DiagnosticEvidenceRequest",
    "EvidenceType",
    "Hypothesis",
    "Diagnosis",
    "Severity",
    "Urgency",
    "decode_vin",
]
