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
    ConsistencyVerdict,
    DiagnosticEvidenceRequest,
    DiagnosticObservation,
    EvidenceType,
    Hypothesis,
    Powertrain,
    ProposedRepair,
    RepairEvidence,
    SafetyEvidence,
    SecondOpinion,
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
from .review import QuoteReviewer, format_second_opinion, looks_like_quote
from .obd import (
    ELM327Client,
    OBDSnapshot,
    decode_dtc,
    observations_from_snapshot,
    parse_dtcs,
    parse_pid,
    parse_vin,
)
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
    "ConsistencyVerdict",
    "ProposedRepair",
    "SecondOpinion",
    "QuoteReviewer",
    "format_second_opinion",
    "looks_like_quote",
    "ELM327Client",
    "OBDSnapshot",
    "observations_from_snapshot",
    "decode_dtc",
    "parse_dtcs",
    "parse_pid",
    "parse_vin",
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
