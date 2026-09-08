"""The end-to-end wizard: plain-English request → ConnectPlan, in one call.

Ties the forgiving front end (:func:`interpret`) to the deterministic back end
(:func:`compile_integration_intent`) across the confirmation boundary. The user's
``answers`` settle the proposal's open questions (authority/identity); confirming seals
the intent; compiling resolves providers and produces the :class:`ConnectPlan`.

    request ──interpret──► IntegrationProposal ──answer+confirm──► ConfirmedIntegrationIntent ──compile──► ConnectPlan

Live execution of the plan (OAuth, real adapters, running the test Mission) is W3/W4 and
needs the connector SDK; this call stops at a fully-resolved, content-addressed plan.
"""
from __future__ import annotations

from typing import Mapping, Optional

from .connect_plan import ConnectPlan, WorkspaceConnections, compile_integration_intent
from .contracts import ConfirmedIntegrationIntent
from .manifest import IntegrationManifest
from .reader import Reader, interpret


def plan_from_request(
    request: str, *, reader: Reader, manifest: IntegrationManifest,
    confirmed_by: str, confirmed_at: str,
    answers: Optional[Mapping[str, str]] = None,
    connections: Optional[WorkspaceConnections] = None,
) -> ConnectPlan:
    """Run the whole deterministic wizard. ``answers`` maps a proposal question to the
    user's answer; an answered question becomes an authority rule and stops blocking the
    confirmation. Raises ``ValueError`` (from ``confirm``) if a question is left
    unanswered — 'we stopped asking' is not 'the user agreed'."""
    intent = confirm_from_request(
        request, reader=reader, manifest=manifest,
        confirmed_by=confirmed_by, confirmed_at=confirmed_at, answers=answers,
    )
    return compile_integration_intent(intent, manifest=manifest, connections=connections)


def confirm_from_request(
    request: str, *, reader: Reader, manifest: IntegrationManifest,
    confirmed_by: str, confirmed_at: str,
    answers: Optional[Mapping[str, str]] = None,
) -> ConfirmedIntegrationIntent:
    """Interpret → answer open questions → confirm. The seal step, split out so a UI can
    show the proposal between reading and confirming."""
    proposal = interpret(request, reader=reader, manifest=manifest)
    answers = dict(answers or {})
    remaining = []
    for q in proposal.questions:
        if q in answers:
            proposal.authority_rules.append(f"{q} -> {answers[q]}")
        else:
            remaining.append(q)
    proposal.questions = remaining
    return proposal.confirm(confirmed_by=confirmed_by, confirmed_at=confirmed_at)
