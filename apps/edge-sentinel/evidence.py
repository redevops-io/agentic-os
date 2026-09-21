"""Edge Sentinel evidence & case spine — the Phase 1 core contracts.

From EDGE_SENTINEL_CAPABILITY_AUDIT plan §"Core contracts": the typed security evidence model that
every later phase (ATT&CK/CTI, hunt, detection engineering, DFIR, response, risk) hangs off. The design
rule the whole plan turns on: **a security signal enters as immutable evidence, and every downstream
claim traces back to it.** So evidence is content-addressed and append-only; observations, findings and
cases reference evidence by id and never mutate it. Rebuilding an observation/finding from the stored
raw evidence is deterministic — that is what makes a case *replayable* (Phase 1 acceptance).

Bitemporality is first-class: everything carries ``observed_at`` (when it happened in the world) and
``known_at`` (when Edge Sentinel learned it), so a replay or a late-arriving datum never rewrites history.

No financial impact is ever invented (see :class:`RiskTranslation`); risk quantification is Agentic
Compliance's job — Edge Sentinel produces technical evidence + a typed handoff.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from enum import Enum


# ──────────────────────────── determinism helpers ────────────────────────────

def canonical_json(obj) -> str:
    """A stable serialization for hashing/replay — sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    elif not isinstance(data, (bytes, bytearray)):
        data = canonical_json(data).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ──────────────────────────── enums ────────────────────────────

class CaseType(str, Enum):
    INCIDENT = "INCIDENT"; HUNT = "HUNT"; DFIR = "DFIR"; CTI = "CTI"
    DETECTION = "DETECTION"; EXPOSURE = "EXPOSURE"; AI_SECURITY = "AI_SECURITY"


class CaseStatus(str, Enum):
    OPEN = "OPEN"; INVESTIGATING = "INVESTIGATING"; CONTAINED = "CONTAINED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"; RESOLVED = "RESOLVED"; CLOSED = "CLOSED"


class Severity(str, Enum):
    CRITICAL = "critical"; HIGH = "high"; MEDIUM = "medium"; LOW = "low"; INFO = "info"


class FindingStatus(str, Enum):
    PROPOSED = "PROPOSED"; SUPPORTED = "SUPPORTED"; CONTRADICTED = "CONTRADICTED"
    CONFIRMED = "CONFIRMED"; DISMISSED = "DISMISSED"


class ActionState(str, Enum):
    PROPOSED = "PROPOSED"; AWAITING_APPROVAL = "AWAITING_APPROVAL"; APPROVED = "APPROVED"
    REJECTED = "REJECTED"; EXECUTED = "EXECUTED"; VERIFIED = "VERIFIED"; FAILED = "FAILED"


# ──────────────────────────── immutable evidence ────────────────────────────

@dataclass(frozen=True)
class EvidenceArtifact:
    """Content-addressed, immutable evidence. ``sha256`` is the digest of the raw content, and
    ``artifact_id`` derives from it — so the same raw datum always yields the same id (idempotent
    ingestion) and any change to the content is a *different* artifact, never an edit. ``chain_of_custody``
    is append-only acquisition provenance."""
    kind: str                       # crowdsec_alert | crowdsec_decision | wazuh_event | file | pcap | ...
    source: str                     # the producing system (e.g. "crowdsec")
    sha256: str
    content_ref: str                # where the raw bytes live (store key / URI)
    acquired_at: str
    known_at: str
    chain_of_custody: tuple[str, ...] = ()
    parser_version: str = "es-evidence-0.1.0"
    derived_from: tuple[str, ...] = ()   # artifact_ids this was derived from (parsing/enrichment)

    @property
    def artifact_id(self) -> str:
        return f"ev-{self.sha256[:16]}"

    @staticmethod
    def of_raw(kind: str, source: str, content, *, observed_at: str = "",
               content_ref: str = "", derived_from: tuple[str, ...] = ()) -> "EvidenceArtifact":
        raw = content if isinstance(content, str) else canonical_json(content)
        digest = sha256_hex(raw)
        now = _now()
        return EvidenceArtifact(
            kind=kind, source=source, sha256=digest,
            content_ref=content_ref or f"evidence://{source}/{digest}",
            acquired_at=observed_at or now, known_at=now,
            chain_of_custody=(f"acquired:{source}@{now}",), derived_from=tuple(derived_from))

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["artifact_id"] = self.artifact_id
        d["chain_of_custody"] = list(self.chain_of_custody)
        d["derived_from"] = list(self.derived_from)
        return d


@dataclass(frozen=True)
class SecurityObservation:
    """A normalized fact derived from evidence. Deterministically re-derivable from ``raw_evidence_ref``
    (that is the replay guarantee). ``id`` derives from the evidence digest + source_type."""
    source: str
    source_type: str                # crowdsec | wazuh | inspector | runtime | ...
    observed_at: str
    known_at: str
    raw_evidence_ref: str           # the EvidenceArtifact.artifact_id this normalizes
    normalized_fields: dict = field(default_factory=dict)
    asset_refs: tuple[str, ...] = ()
    identity_refs: tuple[str, ...] = ()
    network_refs: tuple[str, ...] = ()     # e.g. ("ip:1.2.3.4",)
    artifact_refs: tuple[str, ...] = ()
    detection_refs: tuple[str, ...] = ()
    confidence: float = 1.0

    @property
    def id(self) -> str:
        basis = f"{self.source_type}|{self.raw_evidence_ref}|{sha256_hex(self.normalized_fields)[:16]}"
        return f"obs-{sha256_hex(basis)[:16]}"

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["id"] = self.id
        for k in ("asset_refs", "identity_refs", "network_refs", "artifact_refs", "detection_refs"):
            d[k] = list(getattr(self, k))
        return d


@dataclass(frozen=True)
class Finding:
    """An evidence-backed claim. Carries both supporting AND contradicting evidence (the plan requires
    contradiction to be first-class), plus ATT&CK refs (Phase 2) and recommended actions. A finding with
    no supporting evidence is not allowed to be CONFIRMED — see :meth:`validate`."""
    claim: str
    confidence: float
    evidence_refs: tuple[str, ...] = ()
    contradicting_evidence_refs: tuple[str, ...] = ()
    attack_refs: tuple[str, ...] = ()
    affected_assets: tuple[str, ...] = ()
    recommended_actions: tuple[str, ...] = ()
    status: FindingStatus = FindingStatus.PROPOSED
    created_at: str = field(default_factory=_now)

    @property
    def finding_id(self) -> str:
        return f"fnd-{sha256_hex(self.claim + '|' + '|'.join(sorted(self.evidence_refs)))[:16]}"

    def validate(self) -> bool:
        """A finding may only be CONFIRMED with at least one supporting evidence ref and more support
        than contradiction. Deterministic; no model in the loop."""
        if self.status is FindingStatus.CONFIRMED:
            return bool(self.evidence_refs) and len(self.evidence_refs) > len(self.contradicting_evidence_refs)
        return True

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["finding_id"] = self.finding_id
        d["status"] = self.status.value
        for k in ("evidence_refs", "contradicting_evidence_refs", "attack_refs",
                  "affected_assets", "recommended_actions"):
            d[k] = list(getattr(self, k))
        return d


# ──────────────────────────── governed response (contracts only in P1) ────────────────────────────

@dataclass(frozen=True)
class ActionRequest:
    """A proposed consequential action bound to EXACT evidence/finding versions (so an edit invalidates
    the approval — enforced in Phase 7). In P1 this is the typed record the incident case carries."""
    capability: str                 # e.g. "sentinel.block_ip"
    parameters: dict
    finding_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    approval_required: bool = True
    state: ActionState = ActionState.PROPOSED
    created_at: str = field(default_factory=_now)

    @property
    def request_id(self) -> str:
        basis = canonical_json({"cap": self.capability, "params": self.parameters,
                                "fnd": sorted(self.finding_refs), "ev": sorted(self.evidence_refs)})
        return f"act-{sha256_hex(basis)[:16]}"

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["request_id"] = self.request_id
        d["state"] = self.state.value
        d["finding_refs"] = list(self.finding_refs)
        d["evidence_refs"] = list(self.evidence_refs)
        return d


@dataclass(frozen=True)
class SecurityDecision:
    """The human decision on an ActionRequest — actor + verdict bound to the exact request id."""
    request_id: str
    actor: str
    approved: bool
    rationale: str = ""
    at: str = field(default_factory=_now)

    @property
    def decision_id(self) -> str:
        return f"dec-{sha256_hex(self.request_id + '|' + self.actor + '|' + str(self.approved))[:16]}"

    def to_dict(self) -> dict:
        d = self.__dict__.copy(); d["decision_id"] = self.decision_id; return d


@dataclass(frozen=True)
class ActionReceipt:
    """Proof an action executed — DISTINCT from the decision (the plan requires receipt ≠ verification)."""
    request_id: str
    decision_id: str
    status: str                     # SUCCEEDED | FAILED | HELD
    external_ref: str = ""          # e.g. the CrowdSec decision id created
    error: str = ""
    at: str = field(default_factory=_now)

    @property
    def receipt_id(self) -> str:
        return f"rcpt-{sha256_hex(self.request_id + '|' + self.status + '|' + self.external_ref)[:16]}"

    def to_dict(self) -> dict:
        d = self.__dict__.copy(); d["receipt_id"] = self.receipt_id; return d


# ──────────────────────────── risk handoff (out of Edge Sentinel's authority) ────────────────────────────

@dataclass(frozen=True)
class RiskTranslation:
    """The typed handoff to Agentic Compliance. Edge Sentinel supplies technical basis + evidence;
    it MUST NOT invent financial impact. ``fair_inputs`` stays None here — a helper that tried to
    fabricate one raises. Quantification (FAIR) happens in Compliance, if configured."""
    technical_finding_refs: tuple[str, ...]
    affected_business_service: str = ""
    scenario: str = ""
    likelihood_basis: str = ""          # a description grounded in evidence, never a number Edge invented
    impact_basis: str = ""
    control_refs: tuple[str, ...] = ()
    fair_inputs: dict | None = None     # populated ONLY by Compliance, never by Edge Sentinel
    uncertainty: str = ""
    assumptions: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self):
        if self.fair_inputs is not None:
            # Edge Sentinel is not allowed to originate quantified financial inputs.
            raise ValueError("Edge Sentinel must not invent FAIR/financial inputs; leave fair_inputs=None "
                             "and hand off to Agentic Compliance.")

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        for k in ("technical_finding_refs", "control_refs", "assumptions", "evidence_refs"):
            d[k] = list(getattr(self, k))
        return d


# ──────────────────────────── the case ────────────────────────────

@dataclass
class SecurityCase:
    """A governed investigation. Accumulates references to immutable evidence/observations/findings and
    the response trail (decisions/actions). The case is the only mutable object here; everything it points
    at is frozen, so 'what changed' is always additive and auditable."""
    case_type: CaseType
    severity: Severity
    created_at: str
    known_at: str
    title: str = ""
    status: CaseStatus = CaseStatus.OPEN
    asset_refs: tuple[str, ...] = ()
    observation_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    finding_refs: tuple[str, ...] = ()
    attack_refs: tuple[str, ...] = ()
    intel_refs: tuple[str, ...] = ()
    decision_refs: tuple[str, ...] = ()
    action_refs: tuple[str, ...] = ()
    owner: str = ""
    closure: str = ""
    case_id: str = ""

    def __post_init__(self):
        if not self.case_id:
            basis = f"{self.case_type.value}|{'|'.join(sorted(self.observation_refs)) or self.title}|{self.created_at}"
            self.case_id = f"case-{sha256_hex(basis)[:16]}"

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["case_type"] = self.case_type.value
        d["severity"] = self.severity.value
        d["status"] = self.status.value
        for k in ("asset_refs", "observation_refs", "evidence_refs", "hypotheses", "finding_refs",
                  "attack_refs", "intel_refs", "decision_refs", "action_refs"):
            d[k] = list(getattr(self, k))
        return d
