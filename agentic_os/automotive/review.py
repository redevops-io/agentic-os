"""Second-opinion reviewer — check a shop's proposed repair against the case evidence (plan §6d).

The retention + trust feature: a driver uploads a mechanic's quote/estimate/diagnosis, and the Mission
compares it with the symptoms, DTCs and our own hypotheses already collected — is it reasonable, or is
the shop proposing work the evidence doesn't support (or skipping the likely cause)? Returns a verdict,
the reasons, and the concrete questions to ask before authorizing. Advisory, never a directive.

An injectable LLM does the language work (parse the quote + compare); the verdict vocabulary and the
"never rubber-stamp / always give questions" shape are enforced in code.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from .contracts import (
    ConsistencyVerdict,
    Diagnosis,
    DiagnosticObservation,
    ProposedRepair,
    SecondOpinion,
    SymptomEvidence,
    VehicleRef,
)
from .diagnose import _extract_json, _enum, LLM

SYSTEM_PROMPT = (
    "You are a consumer advocate reviewing a repair shop's proposed work for a driver, comparing it "
    "against the evidence already gathered. Return STRICT JSON only with keys 'verdict', 'reasons', "
    "'questions', 'proposed'. 'verdict' is one of reasonable|unclear|inconsistent. 'reasons' is a list "
    "of short strings explaining the verdict versus the evidence. 'questions' is a list of specific "
    "questions the driver should ask the shop BEFORE authorizing (e.g. confirm which cylinder, whether "
    "a part was tested before replacement). 'proposed' summarizes the quote: {description, parts (list), "
    "labor_hours, cost, shop_name}. Be fair but protective: flag work the evidence does not support and "
    "flag if the likely cause is not addressed. Never tell the driver to simply approve; always give "
    "questions."
)


def _to_proposed(d: Dict[str, Any], raw: str) -> ProposedRepair:
    d = d or {}
    return ProposedRepair(
        description=str(d.get("description", "")), parts=tuple(str(p) for p in d.get("parts", []) or []),
        labor_hours=d.get("labor_hours"), cost=d.get("cost"), shop_name=str(d.get("shop_name", "")),
        raw_text=raw[:2000])


@dataclass
class QuoteReviewer:
    llm: LLM

    def review(self, *, case_id: str, vehicle: VehicleRef, quote_text: str,
               symptoms: Sequence[SymptomEvidence] = (),
               observations: Sequence[DiagnosticObservation] = (),
               diagnosis: Optional[Diagnosis] = None, created_at: str = "") -> SecondOpinion:
        parsed = _extract_json(self.llm(SYSTEM_PROMPT, self._prompt(
            vehicle, symptoms, observations, diagnosis, quote_text)))
        verdict = _enum(ConsistencyVerdict, parsed.get("verdict"), ConsistencyVerdict.UNCLEAR)
        reasons = tuple(str(r) for r in parsed.get("reasons", []) if str(r).strip())
        questions = tuple(str(q) for q in parsed.get("questions", []) if str(q).strip())
        if not questions:  # never rubber-stamp: always give the driver something to ask
            questions = ("Ask the shop to confirm the specific cause and how they diagnosed it.",)
        return SecondOpinion(case_id=case_id, verdict=verdict, reasons=reasons, questions=questions,
                             proposed=_to_proposed(parsed.get("proposed", {}), quote_text),
                             created_at=created_at)

    def _prompt(self, vehicle: VehicleRef, symptoms: Sequence[SymptomEvidence],
                observations: Sequence[DiagnosticObservation], diagnosis: Optional[Diagnosis],
                quote_text: str) -> str:
        lines = [f"Vehicle: {vehicle.label} (powertrain: {vehicle.powertrain.value})"]
        dtcs = [o.code for o in observations if o.kind == "dtc" and o.code]
        if dtcs:
            lines.append("DTCs: " + ", ".join(dtcs))
        for s in symptoms:
            lines.append(f"Symptom: {s.narrative or s.component}")
        if diagnosis and diagnosis.hypotheses:
            lines.append("Our diagnosis (ranked):")
            for h in diagnosis.hypotheses[:3]:
                lines.append(f"  - {h.cause} ({round(float(h.confidence) * 100)}%)")
        lines.append("\nShop's proposed repair / quote:\n" + quote_text.strip())
        lines.append("\nReturn the JSON described in the system prompt.")
        return "\n".join(lines)


# quote/estimate signals for routing an inbound message to the reviewer instead of the diagnoser
_QUOTE_HINTS = re.compile(r"\b(quote|estimate|invoice|labou?r|parts?|\$\s?\d|mechanic said|shop said|"
                          r"they want to|they said i need|diagnos(?:is|ed))\b", re.I)


def looks_like_quote(text: str) -> bool:
    """Heuristic: does this message read like a shop quote/estimate the driver wants reviewed?"""
    if not text:
        return False
    if text.strip().lower().startswith(("/quote", "quote:", "second opinion")):
        return True
    return bool(_QUOTE_HINTS.search(text)) and ("$" in text or re.search(r"\d", text) is not None)


def format_second_opinion(op: SecondOpinion) -> str:
    """Consumer-facing second-opinion message."""
    head = {"reasonable": "✅ *This looks reasonable*",
            "unclear": "❓ *Hard to say from the evidence*",
            "inconsistent": "⚠️ *This may not add up*"}.get(op.verdict.value, "*Second opinion*")
    lines = [head]
    if op.reasons:
        lines.append("")
        lines += [f"- {r}" for r in op.reasons[:4]]
    lines += ["", "*Ask the shop before you approve:*"]
    lines += [f"- {q}" for q in op.questions[:4]]
    lines += ["", "_This is advice to help you decide — not a verdict on the shop._"]
    return "\n".join(lines)
