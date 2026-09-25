"""Legal action governance (moat plan §19.3). A disclaimer is not a boundary — encode it.

This gates EXTERNAL EXECUTION of a legal artifact (send / sign / file), NOT the acquisition of legal research
(that's evidence). Evidence never grants execution permission; source-system authorization stays independent of
legal reasoning. Levels L0–L4 come from the contract.
"""
from __future__ import annotations

from dataclasses import dataclass

from runtime_contracts.protocol import LegalAuthorityLevel, may_auto_execute, requires_professional_review


@dataclass(frozen=True)
class LegalActionDecision:
    allowed: bool
    reason: str
    required: tuple[str, ...] = ()   # what must be satisfied before execution is permitted


def legal_action_decision(level: LegalAuthorityLevel, *, human_approved: bool = False,
                          specialist_evidence: bool = False,
                          professional_reviewed: bool = False) -> LegalActionDecision:
    """May a drafted/legal artifact be executed externally? Encodes §19.3:
      L0 clerical            → auto within ordinary authorization
      L1 approved template   → human approval before send
      L2 novel/jurisdictional→ specialist legal evidence + human review
      L3 judgment/advice     → LEGAL_JUDGMENT_REQUIRED (professional review), never auto
      L4 filing/representation→ professional authorization required
    """
    if may_auto_execute(level):                               # L0
        return LegalActionDecision(True, "clerical — auto within ordinary authorization")
    if level == LegalAuthorityLevel.L1_APPROVED_TEMPLATE:
        return (LegalActionDecision(True, "approved-template draft, human-approved")
                if human_approved else
                LegalActionDecision(False, "human approval required before external execution",
                                    ("human_approval",)))
    if level == LegalAuthorityLevel.L2_NOVEL:
        need = [r for r, ok in (("specialist_legal_evidence", specialist_evidence),
                                ("human_review", human_approved)) if not ok]
        return (LegalActionDecision(True, "novel clause — specialist evidence + human review present")
                if not need else
                LegalActionDecision(False, "novel/jurisdictional draft needs specialist evidence + human review",
                                    tuple(need)))
    # L3 / L4 — consequential; require a recorded professional review/authorization, never evidence alone.
    if requires_professional_review(level):
        label = "professional_authorization" if level == LegalAuthorityLevel.L4_PROFESSIONAL else "professional_review"
        return (LegalActionDecision(True, f"{label} recorded")
                if professional_reviewed else
                LegalActionDecision(False, f"{label} required (LEGAL_JUDGMENT_REQUIRED)", (label,)))
    return LegalActionDecision(False, "unknown level", ())
