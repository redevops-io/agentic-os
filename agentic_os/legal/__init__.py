"""Agentic Apps legal operations (moat plan §19): governed document assistance + specialist legal intelligence.

Native routine drafting from approved templates (L0/L1), an L0–L4 governance gate on external execution, a generic
professional-review handoff, and BYO specialist providers (LexisNexis, CoCounsel) that preserve legal authority.
Not an "AI lawyer": it reduces the professional attention spent on routine assembly + evidence gathering, and
escalates judgment to qualified humans.
"""
from .drafting import DraftArtifact, DraftValidation, draft_document
from .governance import LegalActionDecision, legal_action_decision
from .providers import CoCounselProvider, LexisNexisProvider, legal_artifact_from
from .review import ProfessionalReviewDecision, ProfessionalReviewRequest
from .templates import LegalTemplate, TemplateLibrary, default_library, render

__all__ = [
    "LegalTemplate", "TemplateLibrary", "default_library", "render",
    "DraftArtifact", "DraftValidation", "draft_document",
    "LegalActionDecision", "legal_action_decision",
    "ProfessionalReviewRequest", "ProfessionalReviewDecision",
    "LexisNexisProvider", "CoCounselProvider", "legal_artifact_from",
]
