"""Shop handoff + repair-outcome loop (plan §6e/§7, P4) — the flywheel that builds the moat.

- **Handoff**: turn a case into a structured diagnostic packet a shop receives (vehicle, mileage,
  symptoms, DTCs, leading hypothesis, safety note, requested service) — a qualified, pre-diagnosed lead
  instead of a vague description.
- **Outcome loop**: after the repair, capture what was actually done and whether it fixed the symptom,
  so the confirmed Evidence→Diagnosis→Repair→Outcome graph grows. An injectable LLM parses the driver's
  free-text outcome into a structured RepairEvidence; the vocabulary/verification stay in code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

from .contracts import (
    Diagnosis,
    DiagnosticObservation,
    HandoffPacket,
    RepairEvidence,
    SymptomEvidence,
    VehicleRef,
)
from .diagnose import _extract_json, LLM
from .planner import safety_gate


def build_handoff(*, case_id: str, vehicle: VehicleRef, symptoms: Sequence[SymptomEvidence] = (),
                  observations: Sequence[DiagnosticObservation] = (),
                  diagnosis: Optional[Diagnosis] = None, mileage: str = "",
                  created_at: str = "") -> HandoffPacket:
    top = diagnosis.top if diagnosis else None
    gate = safety_gate(diagnosis.hypotheses) if diagnosis else None
    dtcs = tuple(o.code for o in observations if o.kind == "dtc" and o.code)
    return HandoffPacket(
        case_id=case_id, vehicle=vehicle, mileage=mileage,
        symptoms=tuple(s.narrative for s in symptoms if s.narrative),
        dtcs=dtcs,
        leading_hypothesis=(top.cause if top else ""),
        leading_confidence=(top.confidence if top else "0"),
        safety_note=(gate.advice if gate and not gate.passed else ""),
        requested_service=(top.recommended_action if top and top.recommended_action
                           else (f"Inspect and confirm: {top.cause}" if top else "Diagnose the reported symptoms")),
        created_at=created_at)


def format_handoff(p: HandoffPacket) -> str:
    """Shop-facing handoff (also what the driver can forward)."""
    v = p.vehicle.label if p.vehicle else "Vehicle"
    lines = [f"*Diagnostic handoff — {v}*"]
    if p.mileage:
        lines.append(f"Mileage: {p.mileage}")
    if p.symptoms:
        lines += ["", "*Symptoms*"] + [f"- {s}" for s in p.symptoms[:5]]
    if p.dtcs:
        lines += ["", "*Codes*: " + ", ".join(p.dtcs)]
    if p.leading_hypothesis:
        lines += ["", f"*Leading hypothesis*: {p.leading_hypothesis} "
                  f"({round(float(p.leading_confidence) * 100)}%)"]
    if p.safety_note:
        lines += ["", f"⚠️ {p.safety_note}"]
    lines += ["", f"*Requested service*: {p.requested_service}"]
    lines += ["", f"_Ref {p.packet_id[5:15]} · pre-diagnosed via ReDevOps_"]
    return "\n".join(lines)


_HANDOFF_HINTS = re.compile(r"\b(hand\s?off|to (the |my )?(shop|mechanic|garage)|for (the|my) mechanic|"
                            r"send (this|it|that)? ?to|share with (the|my) (shop|mechanic)|"
                            r"summary for the shop)\b", re.I)
_OUTCOME_HINTS = re.compile(r"\b(fixed|repaired|replaced|resolved|it works now|solved|"
                            r"they (did|replaced|repaired)|no longer|came back|didn'?t (fix|work)|"
                            r"still (happening|there|broken))\b", re.I)


def looks_like_handoff(text: str) -> bool:
    if not text:
        return False
    return text.strip().lower().startswith(("/handoff", "/shop")) or bool(_HANDOFF_HINTS.search(text))


def looks_like_outcome(text: str) -> bool:
    if not text:
        return False
    return text.strip().lower().startswith("/outcome") or bool(_OUTCOME_HINTS.search(text))


SYSTEM_PROMPT = (
    "You extract a repair outcome from a driver's message. Return STRICT JSON only: {diagnosis, "
    "procedure, part, cost, labor_hours, provenance, fixed}. 'fixed' is true if the symptom is gone, "
    "false if it persists/returned, null if unknown. 'provenance' is 'shop' or 'driver'. Use empty "
    "strings / null when a field isn't stated. Do not invent costs."
)


@dataclass
class OutcomeExtractor:
    llm: LLM

    def extract(self, text: str) -> RepairEvidence:
        d = _extract_json(self.llm(SYSTEM_PROMPT, text))
        fixed = d.get("fixed")
        return RepairEvidence(
            diagnosis=str(d.get("diagnosis", "")), procedure=str(d.get("procedure", "")),
            part=str(d.get("part", "")), cost=d.get("cost"), labor_hours=d.get("labor_hours"),
            provenance=str(d.get("provenance", "driver")),
            fixed=(bool(fixed) if isinstance(fixed, bool) else None))


def format_outcome_ack(repair: RepairEvidence) -> str:
    if repair.fixed is True:
        head = "✅ Glad it's sorted — recorded."
    elif repair.fixed is False:
        head = "Thanks — noting it's *not* resolved yet. Tell me what's still happening and we'll dig back in."
    else:
        head = "Thanks — recorded."
    bits = []
    if repair.procedure or repair.part:
        bits.append("Repair: " + " ".join(p for p in (repair.procedure, repair.part) if p))
    if repair.cost:
        bits.append(f"Cost: ${repair.cost}")
    tail = ("\n" + " · ".join(bits)) if bits else ""
    return head + tail + "\n\n_Your outcome helps improve diagnoses for the same fault on your model._"
