"""W2 — interpret a plain-English request into an editable IntegrationProposal.

The forgiving front end. A :class:`Reader` (an injectable interpreter — a deterministic
:class:`KeywordReader` here, a model reader later, same Protocol) turns free text into
candidate capabilities and provider hints. :func:`interpret` then GROUNDS them against the
manifest — it only ever proposes what is buildable, substituting the available provider
and saying so — and asks only what cannot be safely inferred (authority / identity /
irreversible). It never seals: the human confirms the proposal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Protocol, Tuple

from .contracts import CapabilityRequirement, CapabilityRequirementGraph, IntegrationProposal
from .manifest import IntegrationManifest


@dataclass(frozen=True)
class ReaderResult:
    """What an interpreter extracted from a request — candidate, never authoritative."""

    outcome: str
    capabilities: Tuple[str, ...]  # logical capability ids
    provider_hints: Tuple[Tuple[str, str], ...] = ()  # (capability, provider) hints
    edges: Tuple[Tuple[str, str], ...] = ()  # (from_capability, to_capability)


class Reader(Protocol):
    def read(self, request: str) -> ReaderResult: ...


#: Capabilities whose authority/identity cannot be safely guessed → always a question.
AUTHORITY_QUESTIONS: Dict[str, str] = {
    "billing.refund.execute": "Who is allowed to approve refunds?",
    "billing.refund.prepare": "Who is allowed to approve refunds?",
    "chat.message.send": "Which account or number should send replies?",
    "email.message.send": "Which email account should send?",
}


@dataclass
class KeywordReader:
    """A deterministic, offline reader — keyword → (capability, provider hint). The
    drop-in seam for a model reader (same :class:`Reader` protocol). Rules are data, so
    they extend without code."""

    rules: Tuple[Tuple[str, str, str], ...] = ()  # (keyword, capability, provider_hint)

    @staticmethod
    def default() -> "KeywordReader":
        return KeywordReader(
            rules=(
                ("refund", "billing.refund.execute", "stripe"),
                ("charge", "billing.charge.find", "stripe"),
                ("whatsapp", "chat.message.send", "whatsapp_business"),
                ("crm", "crm.contact.upsert", "hubspot"),
                ("hubspot", "crm.contact.upsert", "hubspot"),
                ("salesforce", "crm.contact.upsert", "salesforce"),
                ("slack", "approval.request", "slack"),
                ("approve", "approval.request", "slack"),
                ("email", "email.message.send", "gmail"),
                ("calendar", "calendar.event.create", "google_calendar"),
                ("book", "calendar.event.create", "google_calendar"),
                ("meeting", "calendar.event.create", "google_calendar"),
            )
        )

    def read(self, request: str) -> ReaderResult:
        text = request.lower()
        caps: List[str] = []
        hints: List[Tuple[str, str]] = []
        for kw, cap, prov in self.rules:
            if kw in text and cap not in caps:
                caps.append(cap)
                if prov:
                    hints.append((cap, prov))
        return ReaderResult(
            outcome=request.strip()[:120], capabilities=tuple(caps), provider_hints=tuple(hints)
        )


def interpret(
    request: str, *, reader: Reader, manifest: IntegrationManifest
) -> IntegrationProposal:
    """Read a request, ground it against the manifest, and return an editable proposal.

    Grounding is the load-bearing step: a capability nothing can build is left out (and
    said so honestly), and a provider hint the manifest can't run is substituted for one
    it can. So the proposal a user confirms is always buildable."""
    result = reader.read(request)
    hints = dict(result.provider_hints)
    requirements: List[CapabilityRequirement] = []
    prefs: Dict[str, str] = {}
    assumptions: List[str] = []
    questions: List[str] = []
    seen_q: set[str] = set()

    for cap in result.capabilities:
        hint = hints.get(cap, "")
        provider = manifest.substitute(cap, preferred=hint)
        if provider is None:
            assumptions.append(f"can't do {cap} yet — left it out")
            continue
        if hint and provider != hint:
            assumptions.append(f"{cap}: using {provider} (can't do {hint} yet)")
        elif not hint:
            assumptions.append(f"{cap}: defaulted to {provider}")
        requirements.append(CapabilityRequirement(cap, provider))
        prefs[cap] = provider
        q = AUTHORITY_QUESTIONS.get(cap)
        if q and q not in seen_q:
            questions.append(q)
            seen_q.add(q)

    graph = CapabilityRequirementGraph(
        requirements=tuple(requirements),
        edges=tuple(e for e in result.edges if e[0] in prefs and e[1] in prefs),
    )
    return IntegrationProposal(
        interpreted_outcome=result.outcome, requirements=graph,
        provider_preferences=prefs, assumptions=assumptions, questions=questions,
    )
