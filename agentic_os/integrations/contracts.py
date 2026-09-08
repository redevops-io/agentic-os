"""The two contracts on either side of the confirmation boundary.

:class:`IntegrationProposal` is model-authored and freely editable — it is NOT sealed,
because a human is about to inspect it. Confirming it (:meth:`IntegrationProposal.confirm`)
is the seal event: RAAAL's seal, relocated from fusion-agreement to a person. It produces
a frozen, content-addressed :class:`ConfirmedIntegrationIntent` whose ``content_hash`` is
its identity and whose ``confirmed_by`` records who agreed. Editing after confirmation
makes a new version; the confirmed artifact never mutates.

A :class:`CapabilityRequirementGraph` describes the desired workflow as *logical*
capabilities before any provider is chosen, so provider resolution (a later slice) can
pick Gmail + Google Calendar + HubSpot or Outlook + Microsoft Calendar + Salesforce
against the same requirements. Content-addressing reuses the stack's one seal door,
``runtime_contracts.content_hash``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from runtime_contracts.canonical import content_hash

CONTRACT_VERSION = "integration-intent/0.1"


@dataclass(frozen=True)
class CapabilityRequirement:
    """One logical capability the outcome needs (``crm.contact.upsert``), with an
    optional preferred provider (a *preference*, never authority) and a human reason."""

    capability: str
    provider_preference: str = ""
    reason: str = ""

    def canonical_form(self) -> Dict[str, object]:
        return {
            "capability": self.capability,
            "provider_preference": self.provider_preference,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CapabilityRequirementGraph:
    """Logical capabilities plus ordering edges — the workflow before physical apps."""

    requirements: Tuple[CapabilityRequirement, ...] = ()
    edges: Tuple[Tuple[str, str], ...] = ()  # (from_capability, to_capability)

    def canonical_form(self) -> Dict[str, object]:
        return {
            "requirements": [
                r.canonical_form()
                for r in sorted(self.requirements, key=lambda r: r.capability)
            ],
            "edges": sorted([list(e) for e in self.edges]),
        }

    @property
    def graph_id(self) -> str:
        return content_hash(self.canonical_form())


@dataclass(frozen=True)
class ConfirmedIntegrationIntent:
    """The durable, content-addressed handoff into deterministic planning.

    Created only by :meth:`IntegrationProposal.confirm` — the human confirmation *is*
    the seal. Frozen: an edit after confirmation supersedes with a new intent, it never
    mutates this one. Its ``content_hash`` is over the MEANING only (outcome +
    requirements + preferences + authority + constraints), excluding who and when it was
    confirmed — the same fact regardless of who agreed, matching the stack's rule that
    provenance stays out of identity.
    """

    outcome: str
    requirements: CapabilityRequirementGraph
    provider_preferences: Tuple[Tuple[str, str], ...] = ()  # (capability, provider)
    authority_requirements: Tuple[str, ...] = ()
    workflow_constraints: Tuple[str, ...] = ()
    confirmed_by: str = ""
    confirmed_at: str = ""
    contract_version: str = CONTRACT_VERSION

    def canonical_form(self) -> Dict[str, object]:
        # Meaning only — provenance (who/when confirmed) is excluded from identity.
        return {
            "contract_version": self.contract_version,
            "outcome": self.outcome,
            "requirements": self.requirements.canonical_form(),
            "provider_preferences": sorted([list(p) for p in self.provider_preferences]),
            "authority_requirements": sorted(self.authority_requirements),
            "workflow_constraints": sorted(self.workflow_constraints),
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.canonical_form())


@dataclass
class IntegrationProposal:
    """Model-authored, freely editable, NOT sealed — the forgiving layer.

    It is what the wizard *thinks* the user wants, in a shape the user (or the UI) can
    edit before confirming. ``assumptions`` and ``questions`` are the honest edges of an
    interpretation: assumptions were inferred (reversible things, filled in so the user
    only confirms); questions are what cannot be safely guessed (authority, identity,
    irreversible or genuinely ambiguous choices) and are asked. Neither is compiler
    vocabulary — a UI renders them in business language.
    """

    interpreted_outcome: str = ""
    requirements: CapabilityRequirementGraph = field(default_factory=CapabilityRequirementGraph)
    provider_preferences: Dict[str, str] = field(default_factory=dict)  # capability -> provider
    authority_rules: List[str] = field(default_factory=list)
    workflow_constraints: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)  # inferred (internal provenance)
    questions: List[str] = field(default_factory=list)    # unsafe-to-infer, asked

    def confirm(self, *, confirmed_by: str, confirmed_at: str) -> ConfirmedIntegrationIntent:
        """The seal event: turn this editable proposal into the frozen, content-
        addressed confirmed intent. Refuses while questions remain open — the
        integration analog of RAAAL's seal refusing an unresolved result-changing field
        ('we stopped asking' is not 'the user agreed')."""
        if self.questions:
            raise ValueError(
                "cannot confirm while questions are open: "
                + "; ".join(self.questions)
                + " — answer them (or drop them) before confirming"
            )
        return ConfirmedIntegrationIntent(
            outcome=self.interpreted_outcome,
            requirements=self.requirements,
            provider_preferences=tuple(sorted(self.provider_preferences.items())),
            authority_requirements=tuple(self.authority_rules),
            workflow_constraints=tuple(self.workflow_constraints),
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
        )
