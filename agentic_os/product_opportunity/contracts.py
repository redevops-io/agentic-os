"""Product Opportunity Intelligence — evidence schema (Phase 1).

The unit of discovery is a BROKEN WORKFLOW: work people repeatedly do across apps because the apps don't
complete the business process. Two layers, kept strictly apart (plan §4/§9):

    PainObservation   — what ONE person said, immutable, traceable to a source item
    WorkflowPain      — the NORMALIZED workflow problem many observations resolve into

A PainObservation is a content-addressed, bitemporal ``BusinessObject`` (reusing the Integration-plane base), so
it stores in the same ``ObservationHistory`` as every other observation and its recurrence over time is free.
Raw interpretations can be superseded (re-extracted at a higher ``extraction_version``) without rewriting the
original source evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Tuple

from ..integrations.business.contracts import BusinessObject

SOURCES = ("reddit", "quora", "x")
AUTOMATION_FIT = ("low", "medium", "high")


@dataclass(frozen=True)
class PainObservation(BusinessObject):
    """One source item that appears to describe a workflow problem — immutable evidence. Structured fields are
    an interpretation (``extraction_confidence`` / ``extraction_version``); the raw text stays in
    ``prov.evidence_refs``. ``observed_at`` (we saw it) and ``prov.known_at`` (it was published) are distinct."""
    KIND: ClassVar[str] = "product_opportunity.pain_observation"
    source: str = ""                     # reddit | quora | x
    source_id: str = ""
    source_url: str = ""
    author_hash: str = ""                # stable hash for dedup/independence — NOT an identity dossier
    community_or_topic: str = ""
    published_at: int = 0

    actor: str = ""                      # the role doing the work (ecommerce_operator, bookkeeper, …)
    organization_type: str = ""
    industry: str = ""

    desired_outcome: str = ""
    current_workflow: Tuple[str, ...] = ()
    applications_mentioned: Tuple[str, ...] = ()
    manual_steps: Tuple[str, ...] = ()
    failure_or_pain: str = ""
    consequence: Tuple[str, ...] = ()
    workaround: str = ""
    frequency_hint: str = ""             # daily | weekly | per_order | …  ("" = none stated)
    time_cost_hint: str = ""
    monetary_cost_hint: str = ""
    urgency_hint: str = ""
    willingness_to_pay_signal: bool = False
    cross_app: bool = False              # carries state/context across ≥2 apps (the core signal)

    evidence_strength: float = 0.0       # 0..1, honest proxy (first-person + specificity + frequency + workaround)
    extraction_confidence: float = 0.0
    extraction_version: str = "1"


@dataclass(frozen=True)
class WorkflowPain(BusinessObject):
    """The normalized workflow problem that many PainObservations resolve into (plan §9). PainObservation = what
    one person said; WorkflowPain = the interpreted problem, with its evidence ids."""
    KIND: ClassVar[str] = "product_opportunity.workflow_pain"
    name: str = ""
    actor: str = ""
    desired_outcome: str = ""
    trigger: str = ""
    current_workflow: Tuple[str, ...] = ()
    applications: Tuple[str, ...] = ()
    objects_moved: Tuple[str, ...] = ()
    decisions_required: Tuple[str, ...] = ()
    approvals_required: Tuple[str, ...] = ()
    pain_dimensions: Tuple[str, ...] = ()      # repetitive | manual | time_cost | error_prone | …
    business_consequences: Tuple[str, ...] = ()
    workaround_classes: Tuple[str, ...] = ()
    automation_fit: str = "medium"
    agentic_fit: str = "medium"
    evidence_ids: Tuple[str, ...] = ()
