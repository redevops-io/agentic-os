"""RAAAL-inspired assessment/publication contracts for Evaluation Runtime results.

This layer is intentionally about evidence interpretation, not execution. Assessment records numerical facts;
publication decisions decide what may be claimed on a surface; comparability checks say whether two assessments
can be compared under the same methodology identity.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import math
from typing import Iterable, Mapping

from agentic_os.mission.context_view import content_hash

CONTRACT_VERSION = "assessment-publication/v1"


def _enum_value(value: object) -> str:
    return value.value if isinstance(value, Enum) else str(value)


class PublicationGateStatus(str, Enum):
    PASS = "PASS"
    HOLD = "HOLD"
    PENDING = "PENDING"
    BLOCK = "BLOCK"


class PublicationDecisionValue(str, Enum):
    PASS = "PASS"
    PENDING_LIVE_EVIDENCE = "PENDING_LIVE_EVIDENCE"
    BLOCK = "BLOCK"


class ComparabilityStatus(str, Enum):
    COMPARABLE = "COMPARABLE"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class ErratumStatus(str, Enum):
    OPEN = "OPEN"
    SUPERSEDED = "SUPERSEDED"
    CORRECTED = "CORRECTED"
    RETRACTED = "RETRACTED"


@dataclass(frozen=True)
class EvaluationMethodology:
    """Versioned rules for interpreting a benchmark result.

    ``rules`` are declarations such as "verified outcomes must come from independent readback". Each declared
    rule must have a matching ``realization_checks`` entry; otherwise the methodology is prose, not evidence.
    """

    methodology_id: str
    version: str
    objective: str
    rules: Mapping[str, str]
    realization_checks: Mapping[str, str]
    metric_semantics: Mapping[str, str] = field(default_factory=dict)
    decision_model_identity: Mapping[str, str] = field(default_factory=dict)
    compatibility_dimensions: tuple[str, ...] = ("methodology_semantics",)

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        for name in ("methodology_id", "version", "objective"):
            if not getattr(self, name):
                errors.append(f"{name} is required")
        if not self.rules:
            errors.append("rules are required")
        missing_checks = sorted(set(self.rules) - set(self.realization_checks))
        if missing_checks:
            errors.append(f"realization_checks missing declared rules: {', '.join(missing_checks)}")
        extra_checks = sorted(set(self.realization_checks) - set(self.rules))
        if extra_checks:
            errors.append(f"realization_checks contain undeclared rules: {', '.join(extra_checks)}")
        if not self.compatibility_dimensions:
            errors.append("compatibility_dimensions are required")
        for key in ("provider_id", "model_pin", "score_semantics"):
            if self.decision_model_identity and not self.decision_model_identity.get(key):
                errors.append(f"decision_model_identity.{key} is required")
        return tuple(errors)

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "contract_version": CONTRACT_VERSION,
            "methodology_id": self.methodology_id,
            "version": self.version,
            "objective": self.objective,
            "rules": dict(sorted(self.rules.items())),
            "realization_checks": dict(sorted(self.realization_checks.items())),
            "metric_semantics": dict(sorted(self.metric_semantics.items())),
            "decision_model_identity": dict(sorted(self.decision_model_identity.items())),
            "compatibility_dimensions": sorted(self.compatibility_dimensions),
        }

    def semantic_hash(self) -> str:
        payload = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))
        return content_hash(payload)


@dataclass(frozen=True)
class Assessment:
    """Facts measured from runs. It deliberately carries no publication verdict."""

    assessment_id: str
    suite_id: str
    suite_version: str
    methodology_id: str
    methodology_hash: str
    run_refs: tuple[str, ...]
    metrics: Mapping[str, float]
    trial_count: int
    verified_count: int
    hidden_trial_count: int = 0
    warnings: tuple[str, ...] = ()

    def validation_errors(self, methodology: EvaluationMethodology | None = None) -> tuple[str, ...]:
        errors: list[str] = []
        for name in ("assessment_id", "suite_id", "suite_version", "methodology_id", "methodology_hash"):
            if not getattr(self, name):
                errors.append(f"{name} is required")
        if methodology is not None:
            if methodology.validation_errors():
                errors.append("methodology is invalid")
            if self.methodology_id != methodology.methodology_id:
                errors.append("methodology_id does not match methodology")
            if self.methodology_hash != methodology.semantic_hash():
                errors.append("methodology_hash does not match methodology")
        if self.trial_count < 0:
            errors.append("trial_count must be non-negative")
        if self.verified_count < 0:
            errors.append("verified_count must be non-negative")
        if self.verified_count > self.trial_count:
            errors.append("verified_count must not exceed trial_count")
        if self.hidden_trial_count < 0:
            errors.append("hidden_trial_count must be non-negative")
        if not self.run_refs and self.trial_count:
            errors.append("run_refs are required when trial_count is non-zero")
        for name, value in self.metrics.items():
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                errors.append(f"metric {name!r} must be finite")
        forbidden_metric_names = {"decision", "publication_decision", "publishable", "pass"}
        leaked = sorted(forbidden_metric_names & set(self.metrics))
        if leaked:
            errors.append(f"assessment metrics must not encode publication verdicts: {', '.join(leaked)}")
        return tuple(errors)

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "contract_version": CONTRACT_VERSION,
            "assessment_id": self.assessment_id,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "methodology_id": self.methodology_id,
            "methodology_hash": self.methodology_hash,
            "run_refs": sorted(self.run_refs),
            "metrics": dict(sorted((k, float(v)) for k, v in self.metrics.items())),
            "trial_count": self.trial_count,
            "verified_count": self.verified_count,
            "hidden_trial_count": self.hidden_trial_count,
            "warnings": sorted(self.warnings),
        }

    def digest(self) -> str:
        payload = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))
        return content_hash(payload)


@dataclass(frozen=True)
class PublicationGate:
    id: str
    status: PublicationGateStatus | str
    evidence: str
    summary: str

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        for name in ("id", "evidence", "summary"):
            if not getattr(self, name):
                errors.append(f"{name} is required")
        try:
            PublicationGateStatus(_enum_value(self.status))
        except ValueError:
            errors.append(f"unknown publication gate status {self.status!r}")
        return tuple(errors)

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "status": _enum_value(self.status),
            "evidence": self.evidence,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class PublicationDecision:
    """Surface-aware conclusion derived from gates, not hidden in assessment metrics."""

    surface: str
    suite_id: str
    suite_version: str
    methodology_id: str
    methodology_hash: str
    assessment_digest: str
    gates: tuple[PublicationGate, ...]
    reason: str

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        for name in (
            "surface", "suite_id", "suite_version", "methodology_id", "methodology_hash",
            "assessment_digest", "reason",
        ):
            if not getattr(self, name):
                errors.append(f"{name} is required")
        if not self.gates:
            errors.append("gates are required")
        gate_ids = [gate.id for gate in self.gates]
        if len(set(gate_ids)) != len(gate_ids):
            errors.append("gates contain duplicate ids")
        for gate in self.gates:
            errors.extend(f"gate {gate.id}: {err}" for err in gate.validation_errors())
        return tuple(errors)

    @property
    def blocking(self) -> tuple[str, ...]:
        return tuple(gate.id for gate in self.gates if PublicationGateStatus(_enum_value(gate.status)) is PublicationGateStatus.BLOCK)

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(gate.id for gate in self.gates if PublicationGateStatus(_enum_value(gate.status)) is PublicationGateStatus.PENDING)

    @property
    def decision(self) -> PublicationDecisionValue:
        if self.blocking:
            return PublicationDecisionValue.BLOCK
        if self.pending:
            return PublicationDecisionValue.PENDING_LIVE_EVIDENCE
        return PublicationDecisionValue.PASS

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract_version": CONTRACT_VERSION,
            "surface": self.surface,
            "decision": self.decision.value,
            "publishable_static_bundle": not self.blocking,
            "publishable_full_benchmark_claim": not self.blocking and not self.pending,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "methodology_id": self.methodology_id,
            "methodology_hash": self.methodology_hash,
            "assessment_digest": self.assessment_digest,
            "gates": [gate.as_dict() for gate in self.gates],
            "blocking": list(self.blocking),
            "pending": list(self.pending),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ComparabilityVerdict:
    status: ComparabilityStatus | str
    left_assessment: str
    right_assessment: str
    blockers: tuple[str, ...] = ()

    @property
    def comparable(self) -> bool:
        return ComparabilityStatus(_enum_value(self.status)) is ComparabilityStatus.COMPARABLE

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": CONTRACT_VERSION,
            "status": _enum_value(self.status),
            "comparable": self.comparable,
            "left_assessment": self.left_assessment,
            "right_assessment": self.right_assessment,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class PublicationErratum:
    """Versioned correction record for a published or reviewable artifact."""

    erratum_id: str
    status: ErratumStatus | str
    discovered_at: str
    affected_artifacts: tuple[str, ...]
    reason: str
    correction_summary: str
    replacement_refs: tuple[str, ...] = ()
    supersedes_digest: str = ""
    severity: str = "info"

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        for name in ("erratum_id", "discovered_at", "reason", "correction_summary", "severity"):
            if not getattr(self, name):
                errors.append(f"{name} is required")
        try:
            status = ErratumStatus(_enum_value(self.status))
        except ValueError:
            errors.append(f"unknown erratum status {self.status!r}")
            status = None
        if not self.affected_artifacts:
            errors.append("affected_artifacts are required")
        if len(set(self.affected_artifacts)) != len(self.affected_artifacts):
            errors.append("affected_artifacts contains duplicates")
        if status in (ErratumStatus.SUPERSEDED, ErratumStatus.CORRECTED) and not self.replacement_refs:
            errors.append("replacement_refs are required for corrected or superseded errata")
        if status is ErratumStatus.RETRACTED and not self.supersedes_digest:
            errors.append("supersedes_digest is required for retracted errata")
        if self.severity not in {"info", "low", "medium", "high", "critical"}:
            errors.append(f"unknown erratum severity {self.severity!r}")
        return tuple(errors)

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "contract_version": CONTRACT_VERSION,
            "erratum_id": self.erratum_id,
            "status": _enum_value(self.status),
            "discovered_at": self.discovered_at,
            "affected_artifacts": sorted(self.affected_artifacts),
            "reason": self.reason,
            "correction_summary": self.correction_summary,
            "replacement_refs": sorted(self.replacement_refs),
            "supersedes_digest": self.supersedes_digest,
            "severity": self.severity,
        }

    def digest(self) -> str:
        payload = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))
        return content_hash(payload)


@dataclass(frozen=True)
class ErrataRegister:
    """Append-only errata register for a benchmark publication surface."""

    suite_id: str
    suite_version: str
    surface: str
    entries: tuple[PublicationErratum, ...] = ()

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        for name in ("suite_id", "suite_version", "surface"):
            if not getattr(self, name):
                errors.append(f"{name} is required")
        ids = [entry.erratum_id for entry in self.entries]
        if len(set(ids)) != len(ids):
            errors.append("entries contain duplicate erratum ids")
        for entry in self.entries:
            errors.extend(f"erratum {entry.erratum_id}: {err}" for err in entry.validation_errors())
        return tuple(errors)

    def canonical_payload(self) -> Mapping[str, object]:
        return {
            "schema_version": 1,
            "contract_version": CONTRACT_VERSION,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "surface": self.surface,
            "entry_count": len(self.entries),
            "entries": [entry.canonical_payload() for entry in sorted(self.entries, key=lambda e: e.erratum_id)],
        }

    def digest(self) -> str:
        payload = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))
        return content_hash(payload)


def comparability_verdict(left: Assessment, right: Assessment) -> ComparabilityVerdict:
    blockers: list[str] = []
    if left.methodology_hash != right.methodology_hash:
        blockers.append("methodology_semantic_hash_changed")
    if left.suite_id != right.suite_id:
        blockers.append("suite_id_changed")
    if left.suite_version != right.suite_version:
        blockers.append("suite_version_changed")
    return ComparabilityVerdict(
        status=ComparabilityStatus.NOT_COMPARABLE if blockers else ComparabilityStatus.COMPARABLE,
        left_assessment=left.assessment_id,
        right_assessment=right.assessment_id,
        blockers=tuple(blockers),
    )


def publication_decision_from_gates(
    *,
    surface: str,
    assessment: Assessment,
    gates: Iterable[PublicationGate],
    reason: str,
) -> PublicationDecision:
    return PublicationDecision(
        surface=surface,
        suite_id=assessment.suite_id,
        suite_version=assessment.suite_version,
        methodology_id=assessment.methodology_id,
        methodology_hash=assessment.methodology_hash,
        assessment_digest=assessment.digest(),
        gates=tuple(gates),
        reason=reason,
    )


__all__ = [
    "CONTRACT_VERSION",
    "Assessment",
    "ComparabilityStatus",
    "ComparabilityVerdict",
    "ErrataRegister",
    "ErratumStatus",
    "EvaluationMethodology",
    "PublicationDecision",
    "PublicationErratum",
    "PublicationDecisionValue",
    "PublicationGate",
    "PublicationGateStatus",
    "comparability_verdict",
    "publication_decision_from_gates",
]
