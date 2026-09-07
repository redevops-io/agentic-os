"""Diagnoser — evidence → ranked hypotheses → safety gate → decision-support reply.

The reasoning core of the diagnostic Mission. An injectable LLM callable produces candidate causes from
the collected evidence (symptoms, DTCs, transcribed voice notes, image descriptions); the planner and
safety gate (pure, deterministic) then decide whether more evidence is needed and never let a
safety-critical cause be silently cleared. The LLM proposes, the Runtime disposes — the safety gate and
confidence bar are code, not the model's discretion.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from .contracts import (
    Diagnosis,
    DiagnosticEvidenceRequest,
    DiagnosticObservation,
    EvidenceType,
    Hypothesis,
    Severity,
    SymptomEvidence,
    Urgency,
    VehicleRef,
)
from .planner import safety_gate, should_stop_collecting

# llm(system_prompt, user_prompt) -> model text (expected to contain JSON). Injectable.
LLM = Callable[[str, str], str]

SYSTEM_PROMPT = (
    "You are an expert vehicle-diagnosis assistant helping a driver BEFORE they go to a shop. "
    "Given the vehicle and the evidence, return STRICT JSON only (no prose, no markdown) with keys "
    "'hypotheses' and 'evidence_requests'. Each hypothesis: {cause, confidence (0..1), system "
    "(one of: brakes, steering, airbag, tires, suspension, fuel_leak, overheating, throttle, engine, "
    "electrical, hvac, transmission, exhaust, other), severity (low|medium|high|critical), urgency "
    "(routine|soon|urgent|stop_driving), cost_low, cost_high, recommended_action}. Confidences should "
    "sum to about 1. Each evidence_request: {type (question|photo|video|audio|vin|dtc|obd_snapshot|"
    "human_inspection), prompt, expected_information_gain (0..1), estimated_cost, user_effort "
    "(low|medium|high)}. Be honest about uncertainty; never claim a safety-critical system is fine "
    "without strong evidence."
)


def _extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e != -1:
            text = text[s:e + 1]
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {}


def _enum(cls, value, default):
    try:
        return cls(str(value).lower())
    except (ValueError, AttributeError):
        return default


def _to_hypothesis(d: Dict[str, Any]) -> Optional[Hypothesis]:
    cause = str(d.get("cause", "")).strip()
    if not cause:
        return None
    return Hypothesis(
        cause=cause, confidence=str(d.get("confidence", 0)), system=str(d.get("system", "")).lower(),
        severity=_enum(Severity, d.get("severity"), Severity.MEDIUM),
        urgency=_enum(Urgency, d.get("urgency"), Urgency.SOON),
        cost_low=d.get("cost_low"), cost_high=d.get("cost_high"),
        recommended_action=str(d.get("recommended_action", "")))


def _to_request(d: Dict[str, Any]) -> Optional[DiagnosticEvidenceRequest]:
    t = _enum(EvidenceType, d.get("type"), None)
    if t is None:
        return None
    return DiagnosticEvidenceRequest(
        type=t, prompt=str(d.get("prompt", "")),
        expected_information_gain=str(d.get("expected_information_gain", 0)),
        estimated_cost=str(d.get("estimated_cost", 0)),
        user_effort=str(d.get("user_effort", "low")).lower())


def _have(observations: Sequence[DiagnosticObservation],
          symptoms: Sequence[SymptomEvidence]) -> List[EvidenceType]:
    have: List[EvidenceType] = []
    if any(o.kind == "dtc" for o in observations):
        have.append(EvidenceType.DTC)
    if symptoms:
        have.append(EvidenceType.QUESTION)
    return have


@dataclass
class Diagnoser:
    llm: LLM

    def diagnose(self, *, case_id: str, vehicle: VehicleRef,
                 symptoms: Sequence[SymptomEvidence] = (),
                 observations: Sequence[DiagnosticObservation] = (),
                 created_at: str = "") -> Diagnosis:
        user = self._prompt(vehicle, symptoms, observations)
        parsed = _extract_json(self.llm(SYSTEM_PROMPT, user))
        hyps = [h for h in (_to_hypothesis(x) for x in parsed.get("hypotheses", [])) if h]
        hyps.sort(key=lambda h: float(h.confidence), reverse=True)
        requests = [r for r in (_to_request(x) for x in parsed.get("evidence_requests", [])) if r]

        gate = safety_gate(hyps)
        stop, nxt = should_stop_collecting(hyps, requests, already_have=_have(observations, symptoms))
        # a failed safety gate always keeps the case open for confirming evidence
        needs_more = (not stop) or (not gate.passed and not hyps)
        return Diagnosis(case_id=case_id, vehicle=vehicle, hypotheses=tuple(hyps),
                         safety_gate_passed=gate.passed, needs_more_evidence=needs_more,
                         next_request=(nxt if needs_more else None), created_at=created_at)

    def _prompt(self, vehicle: VehicleRef, symptoms: Sequence[SymptomEvidence],
                observations: Sequence[DiagnosticObservation]) -> str:
        lines = [f"Vehicle: {vehicle.label} (powertrain: {vehicle.powertrain.value})"]
        dtcs = [o.code for o in observations if o.kind == "dtc" and o.code]
        if dtcs:
            lines.append("Diagnostic trouble codes: " + ", ".join(dtcs))
        for o in observations:
            if o.kind != "dtc":
                lines.append(f"Observation {o.kind}: {o.code or ''} {o.value} {o.unit}".strip())
        for s in symptoms:
            desc = s.narrative or s.component
            if s.mileage:
                desc += f" (mileage {s.mileage})"
            lines.append(f"Symptom: {desc}")
        lines.append("Return the JSON described in the system prompt.")
        return "\n".join(lines)


def format_reply(diagnosis: Diagnosis) -> str:
    """The consumer-facing decision-support message (plan §5), for Telegram/WhatsApp."""
    hyps = diagnosis.hypotheses
    if not hyps:
        nxt = diagnosis.next_request
        ask = f"\n\nTo narrow it down: {nxt.prompt}" if nxt and nxt.prompt else ""
        return "I need a bit more to diagnose this." + ask
    top = hyps[0]
    gate = safety_gate(hyps)
    lines = ["*What's most likely*",
             f"{top.cause} — {round(float(top.confidence) * 100)}%"]
    others = [f"- {h.cause} — {round(float(h.confidence) * 100)}%" for h in hyps[1:3]]
    if others:
        lines.append("Other possibilities:")
        lines += others
    urgency = {"routine": "Routine", "soon": "Address soon", "urgent": "URGENT",
               "stop_driving": "STOP DRIVING"}.get(top.urgency.value, "Address soon")
    lines += ["", f"*How urgent?* {urgency}"]
    if top.recommended_action:
        lines += ["", f"*Likely first step*", top.recommended_action]
    if top.cost_low and top.cost_high:
        lines += ["", f"*Expected shop range* ${top.cost_low}–${top.cost_high}"]
    if not gate.passed:
        lines += ["", f"⚠️ {gate.advice}"]
    if diagnosis.needs_more_evidence and diagnosis.next_request and diagnosis.next_request.prompt:
        lines += ["", f"To be more sure: {diagnosis.next_request.prompt}"]
    else:
        lines += ["", "*Before you authorize repair* — ask the shop to confirm the specific cause and "
                  "that they tested it before replacing parts."]
    return "\n".join(lines)
