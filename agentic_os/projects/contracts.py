"""Canonical Projects contracts — the generic human/work objects over durable Missions.

From REDEVOPS_PROJECTS_UI_AGENTIC_APPS_AUDIT plan §4/§8: Projects is the generic workspace over Missions,
not app-specific dashboards. Every human-relevant Mission output is an Artifact; every human dependency is
a structured HumanRequest; a Decision records the human response; approval is NOT execution — an
ActionRequest runs through governance to a capability and yields an ActionReceipt, and only a confirmed
receipt flips an artifact to PUBLISHED/SENT/APPLIED. These are storage-agnostic dataclasses shared by
Sidekick and Projects (§12) so both reference the SAME objects, never copies.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class ArtifactStatus(str, Enum):
    DRAFT = "DRAFT"; GENERATING = "GENERATING"; READY = "READY"; NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"; REJECTED = "REJECTED"; HELD = "HELD"; EXECUTING = "EXECUTING"
    PUBLISHED = "PUBLISHED"; SENT = "SENT"; APPLIED = "APPLIED"; FAILED = "FAILED"; SUPERSEDED = "SUPERSEDED"


class LifecycleClass(str, Enum):
    EPHEMERAL = "EPHEMERAL"; INTERMEDIATE_VISIBLE = "INTERMEDIATE_VISIBLE"; REVIEWABLE = "REVIEWABLE"
    FINAL = "FINAL"; EXTERNAL_RECEIPT = "EXTERNAL_RECEIPT"; OUTCOME = "OUTCOME"


class HumanGate(str, Enum):
    """§5 gate taxonomy — what KIND of human decision this is (drives presentation + policy)."""
    G0_NONE = "G0"; G1_INFO = "G1"; G2_REVIEW = "G2"; G3_BUSINESS = "G3"
    G4_EXTERNAL_COMMS = "G4"; G5_COMMITMENT = "G5"; G6_DESTRUCTIVE = "G6"


class HumanRequestType(str, Enum):
    PROVIDE_INFORMATION = "PROVIDE_INFORMATION"; CHOOSE_OPTION = "CHOOSE_OPTION"; REVIEW = "REVIEW"
    APPROVE = "APPROVE"; REJECT = "REJECT"; EDIT = "EDIT"; CONFIRM = "CONFIRM"; ASSIGN = "ASSIGN"
    RESOLVE_CONFLICT = "RESOLVE_CONFLICT"; AUTHORIZE_EXTERNAL_ACTION = "AUTHORIZE_EXTERNAL_ACTION"


@dataclass
class Artifact:
    """A durable, versioned Mission output the human sees, reviews, and authorizes (§4)."""
    mission_id: str
    app_id: str
    type: str                      # SocialPost | Video | Text | Image | ... (renderer registry §10)
    title: str
    project_id: str = ""
    subtype: str = ""              # e.g. channel: x | linkedin | tiktok
    summary: str = ""
    content: str = ""              # inline text content (or use content_ref for blobs)
    content_ref: str = ""
    mime_type: str = ""
    preview_ref: str = ""          # a viewable preview URL/path; "" when unavailable (e.g. in-cluster media)
    status: ArtifactStatus = ArtifactStatus.DRAFT
    lifecycle_class: LifecycleClass = LifecycleClass.REVIEWABLE
    version: int = 1
    parent_version_id: str = ""
    allowed_actions: tuple[str, ...] = ()      # e.g. ("Edit","Approve") | ("Edit brief","Regenerate")
    hold_reason: str = ""
    evidence_refs: tuple[str, ...] = ()
    lineage_refs: tuple[str, ...] = ()         # §15 — solicitation → revenue mission → content mission → this
    artifact_id: str = field(default_factory=lambda: _id("art"))
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["status"] = self.status.value
        d["lifecycle_class"] = self.lifecycle_class.value
        d["allowed_actions"] = list(self.allowed_actions)
        d["evidence_refs"] = list(self.evidence_refs)
        d["lineage_refs"] = list(self.lineage_refs)
        return d


@dataclass
class HumanRequest:
    """A structured ask for a human (§4) — not every interaction is an approval."""
    mission_id: str
    type: HumanRequestType
    prompt: str
    gate: HumanGate = HumanGate.G2_REVIEW
    artifact_ids: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    consequences: str = ""         # §11 — the consequence of an irreversible action, shown BEFORE approval
    status: str = "OPEN"
    request_id: str = field(default_factory=lambda: _id("req"))
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["type"] = self.type.value
        d["gate"] = self.gate.value
        d["artifact_ids"] = list(self.artifact_ids)
        d["options"] = list(self.options)
        return d


@dataclass
class Decision:
    """The exact human response (§4/§14) — actor + action + the EXACT artifact versions it binds."""
    request_id: str
    actor: str
    action: str                    # approve | reject | edit | ...
    selected: tuple[tuple[str, int], ...] = ()    # (artifact_id, version) pairs it authorizes — exact
    modifications: dict = field(default_factory=dict)
    decision_id: str = field(default_factory=lambda: _id("dec"))
    at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["selected"] = [list(x) for x in self.selected]
        return d


class WorkflowStatus(str, Enum):
    """The lifecycle of a learned/authored WorkflowDefinition (Workflow-Teaching plan §4). An ACTIVE
    version is immutable — an amendment creates a NEW version, never a mutation in place."""
    DRAFT = "DRAFT"; NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"; READY_FOR_REVIEW = "READY_FOR_REVIEW"
    VALIDATING = "VALIDATING"; SHADOW = "SHADOW"; ACTIVE = "ACTIVE"; PAUSED = "PAUSED"
    SUPERSEDED = "SUPERSEDED"; RETIRED = "RETIRED"


class RuleProvenance(str, Enum):
    """§7/§14 — every material workflow rule must expose how it is known. A LEARNED rule is INFERRED
    until a human confirms it; POLICY_DEFINED rules are authored (policy beats learned behavior, §18.5)."""
    OBSERVED = "OBSERVED"; INFERRED = "INFERRED"; HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
    POLICY_DEFINED = "POLICY_DEFINED"; UNKNOWN = "UNKNOWN"


class CandidateStatus(str, Enum):
    """§10 — a human decision does not silently become behavior; it moves through this."""
    PROPOSED = "PROPOSED"; ACCEPTED = "ACCEPTED"; REJECTED = "REJECTED"
    NEEDS_MORE_EVIDENCE = "NEEDS_MORE_EVIDENCE"


@dataclass
class WorkflowRule:
    """One inspectable rule inside a workflow — carries its provenance so the UI can label it (§14) and
    an optional human gate (a rule may itself require approval). A learned rule cannot silently relax a
    gate a POLICY_DEFINED rule requires (§18)."""
    intent: str
    statement: str
    provenance: RuleProvenance = RuleProvenance.INFERRED
    scope: str = ""
    gate: HumanGate = HumanGate.G0_NONE
    evidence_refs: tuple[str, ...] = ()
    confidence: float = 0.0
    rule_id: str = field(default_factory=lambda: _id("rule"))

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["provenance"] = self.provenance.value
        d["gate"] = self.gate.value
        d["evidence_refs"] = list(self.evidence_refs)
        return d


@dataclass
class WorkflowDefinition:
    """A reusable, versioned execution definition (§4). ACTIVE versions are IMMUTABLE (§18.8/§25.11): an
    amendment produces a NEW version with ``parent_version_id`` set, never a mutation in place. Every rule
    exposes its provenance (§25.3); a workflow with material UNKNOWNs cannot activate unless policy permits
    abstention there (§14)."""
    project_id: str
    title: str
    description: str = ""
    app_ids: tuple[str, ...] = ()
    trigger: str = ""
    rules: tuple[WorkflowRule, ...] = ()
    human_gates: tuple[HumanGate, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    learned_from: dict = field(default_factory=dict)   # {recording_refs, mission_refs, decision_refs}
    status: WorkflowStatus = WorkflowStatus.DRAFT
    version: int = 1
    parent_version_id: str = ""
    workflow_id: str = field(default_factory=lambda: _id("wf"))
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def is_active(self) -> bool:
        return self.status is WorkflowStatus.ACTIVE

    @property
    def has_blocking_unknowns(self) -> bool:
        return bool(self.unresolved_questions)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["app_ids"] = list(self.app_ids)
        d["rules"] = [r.to_dict() for r in self.rules]
        d["human_gates"] = [g.value for g in self.human_gates]
        d["unresolved_questions"] = list(self.unresolved_questions)
        d["status"] = self.status.value
        return d


@dataclass
class WorkflowLearningCandidate:
    """§10 — a proposed workflow change awaiting validation. Serializable (no engine handle): it records
    the proposed rule, its scope, the source decisions/missions, and the support/confidence the Discovery
    Learn engine computed — so "why does the system believe this?" is answerable, and acceptance is a
    governed, versioned step (§25.9/§25.10)."""
    project_id: str
    workflow_id: str
    proposed_rule: WorkflowRule
    scope: str = ""
    source_decision_refs: tuple[str, ...] = ()
    source_mission_refs: tuple[str, ...] = ()
    supporting: int = 0
    counterexamples: int = 0
    confidence: float = 0.0            # the engine's Wilson lower bound over (supporting, counterexamples)
    status: CandidateStatus = CandidateStatus.PROPOSED
    resulting_workflow_version: str = ""   # the new WorkflowDefinition id, once ACCEPTED
    rationale: str = ""
    learning_id: str = field(default_factory=lambda: _id("learn"))
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["proposed_rule"] = self.proposed_rule.to_dict()
        d["source_decision_refs"] = list(self.source_decision_refs)
        d["source_mission_refs"] = list(self.source_mission_refs)
        d["status"] = self.status.value
        return d


@dataclass
class ActionReceipt:
    """Proof an external action occurred (§4) — the artifact only flips to PUBLISHED once this exists."""
    artifact_id: str
    capability: str
    provider: str
    status: str                    # SUCCEEDED | FAILED | HELD
    external_id: str = ""
    external_url: str = ""
    error: str = ""
    decision_id: str = ""
    receipt_id: str = field(default_factory=lambda: _id("rcpt"))
    at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return self.__dict__.copy()
