"""Generic professional-review handoff (moat plan §19.5).

When a legal decision exceeds what the Runtime may resolve (L2+), it hands a structured request to a qualified
professional rather than encoding one firm/vendor. The returned decision becomes GOVERNED EVIDENCE — not an
invisible chat message — so it can gate execution via governance.legal_action_decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from runtime_contracts.protocol import content_hash


@dataclass(frozen=True)
class ProfessionalReviewRequest:
    question: str
    business_context: str
    relevant_artifacts: tuple[str, ...] = ()      # artifact ids / refs
    change_path: str = ""                          # dependency/impact path (e.g. from Semantic Change Closure)
    authorities_retrieved: tuple[str, ...] = ()    # legal authority refs already gathered
    unresolved_issue: str = ""
    proposed_action: str = ""
    deadline: str = ""

    def request_id(self) -> str:
        return content_hash(self.canonical_form())

    def canonical_form(self) -> dict:
        return {
            "question": self.question, "business_context": self.business_context,
            "relevant_artifacts": list(self.relevant_artifacts), "change_path": self.change_path,
            "authorities_retrieved": list(self.authorities_retrieved), "unresolved_issue": self.unresolved_issue,
            "proposed_action": self.proposed_action, "deadline": self.deadline,
        }


@dataclass(frozen=True)
class ProfessionalReviewDecision:
    request_id: str
    reviewer: str                 # verified professional principal
    decision: str                 # approved | rejected | approved_with_conditions
    rationale: str = ""
    conditions: tuple[str, ...] = ()
    decided_at: str = ""

    def approved(self) -> bool:
        return self.decision in ("approved", "approved_with_conditions")

    def canonical_form(self) -> dict:
        return {
            "request_id": self.request_id, "reviewer": self.reviewer, "decision": self.decision,
            "rationale": self.rationale, "conditions": list(self.conditions), "decided_at": self.decided_at,
        }
