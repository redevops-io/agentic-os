"""Car-diagnosis domain contracts — the evidence → diagnosis → repair → outcome graph.

Frozen, content-addressed where evidence provenance matters (same ``rcv1`` hashing as the rest of the
stack). Fractional values (confidence, cost, information gain) are carried as **decimal strings**, not
floats, so hashes agree across languages and Doris/S3 round-trips are exact. Nothing here decides a
diagnosis — these are the data the Mission reasons over; the safety gate and evidence planner act on
them (see the plan §6a/§6c).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from runtime_contracts.canonical import content_hash, decimal_string

CONTRACT_VERSION = "car-diagnosis/v1"


class Powertrain(str, Enum):
    ICE = "ice"
    HYBRID = "hybrid"
    PHEV = "phev"
    EV = "ev"
    UNKNOWN = "unknown"


class EvidenceType(str, Enum):
    """What the evidence planner can ask for next, cheapest/lowest-effort first is a runtime decision."""

    QUESTION = "question"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VIN = "vin"
    DTC = "dtc"
    OBD_SNAPSHOT = "obd_snapshot"
    OBD_LIVE_PID = "obd_live_pid"
    HUMAN_INSPECTION = "human_inspection"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Urgency(str, Enum):
    ROUTINE = "routine"
    SOON = "soon"
    URGENT = "urgent"
    STOP_DRIVING = "stop_driving"


#: components/systems that are safety-critical — a hypothesis touching one cannot be silently cleared.
SAFETY_CRITICAL_SYSTEMS = frozenset({
    "brakes", "steering", "airbag", "srs", "tires", "suspension", "fuel_leak",
    "overheating", "throttle", "misfire_flashing_cel", "seatbelt", "lighting",
})


def _dec(value: Optional[Any]) -> Optional[str]:
    """A fractional value as a decimal string (floats are refused by canonical hashing)."""
    if value is None:
        return None
    return decimal_string(value if isinstance(value, str) else repr(value))


@dataclass(frozen=True)
class VehicleRef:
    """Vehicle identity, normally decoded from a VIN via NHTSA vPIC. Makes everything model-specific —
    the thing a generic chatbot cannot do."""

    vin: str = ""
    make: str = ""
    model: str = ""
    year: str = ""
    powertrain: Powertrain = Powertrain.UNKNOWN
    body_class: str = ""
    fuel_primary: str = ""
    engine_cylinders: str = ""
    displacement_l: str = ""

    def canonical_form(self) -> Dict[str, Any]:
        return {"vin": self.vin.upper(), "make": self.make, "model": self.model, "year": self.year,
                "powertrain": self.powertrain.value, "body_class": self.body_class,
                "fuel_primary": self.fuel_primary, "engine_cylinders": self.engine_cylinders,
                "displacement_l": self.displacement_l}

    @property
    def label(self) -> str:
        return " ".join(p for p in (self.year, self.make, self.model) if p) or (self.vin or "unknown vehicle")


@dataclass(frozen=True)
class DiagnosticObservation:
    """A machine-read fact: a DTC, or a sensor/PID value. From an OBD scan, a photo of a code, or telemetry."""

    kind: str                       # "dtc" | "pid" | "sensor" | "freeze_frame"
    code: str = ""                  # e.g. "P0302"
    value: str = ""                 # decimal string / text for a PID value
    unit: str = ""
    source: str = ""                # "obd" | "photo" | "user" | "edge_node"
    observed_at: str = ""

    def canonical_form(self) -> Dict[str, Any]:
        return {"contract_version": CONTRACT_VERSION, "kind": self.kind, "code": self.code,
                "value": self.value, "unit": self.unit, "source": self.source,
                "observed_at": self.observed_at}

    @property
    def evidence_id(self) -> str:
        return content_hash(self.canonical_form())


@dataclass(frozen=True)
class SymptomEvidence:
    """A human-reported symptom + the media that backs it (referenced by hash, never inlined)."""

    component: str = ""
    narrative: str = ""             # what the driver said (from text or transcribed voice note)
    mileage: str = ""
    media_refs: Tuple[str, ...] = ()   # MediaArtifact/InteractionEvent content hashes
    observed_at: str = ""

    def canonical_form(self) -> Dict[str, Any]:
        return {"contract_version": CONTRACT_VERSION, "component": self.component,
                "narrative": self.narrative, "mileage": self.mileage,
                "media_refs": sorted(self.media_refs), "observed_at": self.observed_at}

    @property
    def evidence_id(self) -> str:
        return content_hash(self.canonical_form())


@dataclass(frozen=True)
class SafetyEvidence:
    """A recall / investigation / TSB bearing on the vehicle (from NHTSA)."""

    kind: str                       # "recall" | "investigation" | "tsb" | "complaint"
    ref_id: str = ""
    title: str = ""
    source: str = "nhtsa"

    def canonical_form(self) -> Dict[str, Any]:
        return {"kind": self.kind, "ref_id": self.ref_id, "title": self.title, "source": self.source}


@dataclass(frozen=True)
class RepairEvidence:
    """A repair action + outcome — the flywheel's payload once a shop confirms what fixed it."""

    diagnosis: str = ""
    procedure: str = ""
    part: str = ""
    labor_hours: Optional[str] = None
    cost: Optional[str] = None
    provenance: str = ""            # "shop" | "driver" | "edge_verified"
    fixed: Optional[bool] = None    # did the symptom disappear?

    def __post_init__(self) -> None:
        object.__setattr__(self, "labor_hours", _dec(self.labor_hours))
        object.__setattr__(self, "cost", _dec(self.cost))


@dataclass(frozen=True)
class DiagnosticEvidenceRequest:
    """The evidence planner's proposal for the next observation to acquire (plan §6a). The runtime picks
    the request maximising expected value per unit of burden, subject to safety."""

    type: EvidenceType
    prompt: str = ""                        # what to ask the user / capability to invoke
    expected_information_gain: str = "0"    # decimal string 0..1
    estimated_cost: str = "0"               # decimal string (USD or normalized)
    user_effort: str = "low"                # low | medium | high
    latency: str = "low"
    safety_risk: str = "none"
    availability: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_information_gain", _dec(self.expected_information_gain) or "0")
        object.__setattr__(self, "estimated_cost", _dec(self.estimated_cost) or "0")

    @property
    def value_per_burden(self) -> float:
        """Ranking score: expected information gain ÷ (cost + effort weight). Higher = ask this next.
        Deterministic float for ordering only — never hashed."""
        effort = {"low": 0.1, "medium": 0.5, "high": 1.0}.get(self.user_effort, 0.5)
        burden = float(self.estimated_cost) + effort + 1e-6
        return float(self.expected_information_gain) / burden


@dataclass(frozen=True)
class Hypothesis:
    """One ranked possible cause with its supporting evidence and consumer-facing guidance."""

    cause: str
    confidence: str = "0"                   # decimal string 0..1
    system: str = ""                        # normalized system, for the safety gate
    evidence_refs: Tuple[str, ...] = ()     # evidence_ids that support it
    severity: Severity = Severity.MEDIUM
    urgency: Urgency = Urgency.SOON
    cost_low: Optional[str] = None
    cost_high: Optional[str] = None
    recommended_action: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", _dec(self.confidence) or "0")
        object.__setattr__(self, "cost_low", _dec(self.cost_low))
        object.__setattr__(self, "cost_high", _dec(self.cost_high))

    @property
    def is_safety_critical(self) -> bool:
        return self.system in SAFETY_CRITICAL_SYSTEMS or self.severity is Severity.CRITICAL

    def canonical_form(self) -> Dict[str, Any]:
        return {"cause": self.cause, "confidence": self.confidence, "system": self.system,
                "evidence_refs": sorted(self.evidence_refs), "severity": self.severity.value,
                "urgency": self.urgency.value, "cost_low": self.cost_low, "cost_high": self.cost_high,
                "recommended_action": self.recommended_action}


@dataclass(frozen=True)
class Diagnosis:
    """A ranked set of hypotheses for one case, with the safety-gate result and the planner's next ask.
    Content-addressed so a case's diagnoses are an auditable, replayable trail."""

    case_id: str
    vehicle: Optional[VehicleRef] = None
    hypotheses: Tuple[Hypothesis, ...] = ()
    safety_gate_passed: bool = False
    needs_more_evidence: bool = True
    next_request: Optional[DiagnosticEvidenceRequest] = None
    created_at: str = ""
    contract_version: str = CONTRACT_VERSION

    @property
    def top(self) -> Optional[Hypothesis]:
        return max(self.hypotheses, key=lambda h: float(h.confidence), default=None)

    def canonical_form(self) -> Dict[str, Any]:
        return {"contract_version": self.contract_version, "case_id": self.case_id,
                "vehicle": self.vehicle.canonical_form() if self.vehicle else None,
                "hypotheses": [h.canonical_form() for h in self.hypotheses],
                "safety_gate_passed": self.safety_gate_passed,
                "needs_more_evidence": self.needs_more_evidence, "created_at": self.created_at}

    @property
    def diagnosis_id(self) -> str:
        return content_hash(self.canonical_form())
