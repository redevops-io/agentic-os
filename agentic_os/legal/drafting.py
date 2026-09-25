"""Native document drafting (moat plan §19.1/§19.4): approved template + business facts → validated draft.

Flow: business facts → approved template → draft artifact → deterministic validation → (review) → APPROVED
Decision → governed send. This module does the draft + validation; execution is gated by governance.py. The
system never converts a drafting request into a legal judgment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from runtime_contracts.protocol import LegalAuthorityLevel, content_hash, may_auto_execute

from .templates import TemplateLibrary, render

_PLACEHOLDER = re.compile(r"{(\w+)}")


@dataclass(frozen=True)
class DraftValidation:
    ok: bool
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class DraftArtifact:
    document_id: str
    template_id: str
    level: LegalAuthorityLevel
    jurisdiction: str
    body: str
    facts: dict
    validation: DraftValidation
    needs_review: bool          # true unless the level is clerical/auto
    auto_executable: bool       # may be sent within ordinary authorization (L0 + valid only)

    def canonical_form(self) -> dict:
        return {
            "document_id": self.document_id, "template_id": self.template_id, "level": int(self.level),
            "jurisdiction": self.jurisdiction, "body": self.body, "facts": self.facts,
            "validation": {"ok": self.validation.ok, "issues": list(self.validation.issues)},
            "needs_review": self.needs_review, "auto_executable": self.auto_executable,
        }


def draft_document(library: TemplateLibrary, template_id: str, facts: dict, *,
                   jurisdiction: str = "") -> DraftArtifact:
    template = library.get(template_id)
    if template is None:
        raise KeyError(f"no approved template {template_id!r}")
    issues: list[str] = []
    try:
        body = render(template, facts)
    except ValueError as e:
        body = ""
        issues.append(str(e))
    leftover = _PLACEHOLDER.findall(body)
    if leftover:
        issues.append(f"unfilled placeholders: {sorted(set(leftover))}")
    validation = DraftValidation(ok=not issues, issues=tuple(issues))
    return DraftArtifact(
        document_id=content_hash({"tid": template_id, "facts": facts, "juris": jurisdiction}),
        template_id=template_id, level=template.level,
        jurisdiction=jurisdiction or template.jurisdiction, body=body, facts=dict(facts),
        validation=validation, needs_review=not may_auto_execute(template.level),
        auto_executable=may_auto_execute(template.level) and validation.ok)
