"""Organization-approved legal templates (moat plan §19.1 native routine document assistance).

The stack drafts low-complexity documents ONLY from approved templates + supplied facts — never by inventing
legal language. Each template declares its authority LEVEL (L0 clerical / L1 approved-template), which drives the
governance gate for any external execution (see governance.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from runtime_contracts.protocol import LegalAuthorityLevel

_PLACEHOLDER = re.compile(r"{(\w+)}")


@dataclass(frozen=True)
class LegalTemplate:
    template_id: str
    title: str
    level: LegalAuthorityLevel
    body: str                                  # contains {placeholders}
    required_fields: tuple[str, ...] = ()
    jurisdiction: str = ""

    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.body))


class TemplateLibrary:
    """Only organization-approved templates live here (§19.1). Precedents are added deliberately, not generated."""

    def __init__(self) -> None:
        self._t: dict[str, LegalTemplate] = {}

    def register(self, template: LegalTemplate) -> LegalTemplate:
        self._t[template.template_id] = template
        return template

    def get(self, template_id: str) -> LegalTemplate | None:
        return self._t.get(template_id)

    def all(self) -> tuple[LegalTemplate, ...]:
        return tuple(self._t.values())


def render(template: LegalTemplate, facts: dict) -> str:
    """Deterministically fill an approved template. Raises on a missing required field or unknown placeholder —
    the system must never silently paper over an incomplete draft."""
    missing = [f for f in template.required_fields if not str(facts.get(f, "")).strip()]
    if missing:
        raise ValueError(f"missing required fields: {sorted(missing)}")
    unknown = template.placeholders() - set(facts)
    if unknown:
        raise ValueError(f"template has placeholders with no supplied fact: {sorted(unknown)}")
    return template.body.format(**{k: facts[k] for k in template.placeholders()})


def default_library() -> TemplateLibrary:
    """A tiny starter set of L1 approved templates. Real deployments load the organization's own precedents."""
    lib = TemplateLibrary()
    lib.register(LegalTemplate(
        template_id="nda_mutual", title="Mutual NDA", level=LegalAuthorityLevel.L1_APPROVED_TEMPLATE,
        required_fields=("party_a", "party_b", "effective_date", "term_years", "governing_law"),
        body=("MUTUAL NON-DISCLOSURE AGREEMENT\n\nThis Agreement is entered into as of {effective_date} between "
              "{party_a} and {party_b}. Each party may disclose Confidential Information to the other solely to "
              "evaluate a potential business relationship. The receiving party shall protect such information for "
              "{term_years} year(s) and use it only for that purpose. This Agreement is governed by the laws of "
              "{governing_law}.")))
    lib.register(LegalTemplate(
        template_id="sow", title="Statement of Work", level=LegalAuthorityLevel.L1_APPROVED_TEMPLATE,
        required_fields=("client", "provider", "scope", "fee", "start_date"),
        body=("STATEMENT OF WORK\n\nClient: {client}\nProvider: {provider}\nStart date: {start_date}\n\nScope of "
              "services:\n{scope}\n\nFees: {fee}. This SOW incorporates the parties' governing services agreement.")))
    lib.register(LegalTemplate(
        template_id="invoice_demand", title="Invoice Demand Letter", level=LegalAuthorityLevel.L1_APPROVED_TEMPLATE,
        required_fields=("debtor", "creditor", "invoice_number", "amount", "due_date"),
        body=("Re: Overdue Invoice {invoice_number}\n\nDear {debtor},\n\nOur records show invoice {invoice_number} "
              "for {amount}, due {due_date}, remains unpaid. We request payment within 14 days. Regards,\n"
              "{creditor}")))
    return lib
