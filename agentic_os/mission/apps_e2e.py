"""Agentic Apps E2E benchmark contract (apps-e2e/v1).

This is the small public contract layer for the unified Agentic Apps benchmark. It deliberately does not
run real cores or score business outcomes; it pins the vocabulary that later runners, oracles and public
Reliability Lab artifacts use: app coverage, decision-model lanes, scenario identity and publishable
coverage summaries.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
from typing import Iterable, Mapping

from agentic_os.mission.context_view import content_hash

CONTRACT_VERSION = "apps-e2e/v1"


@dataclass(frozen=True)
class AppCore:
    id: str
    product: str
    role: str
    required: bool = True


APP_CORES: tuple[AppCore, ...] = (
    AppCore("twenty", "Twenty", "CRM"),
    AppCore("chatwoot", "Chatwoot", "support"),
    AppCore("erpnext", "ERPNext", "ERP/books"),
    AppCore("lago", "Lago", "billing"),
    AppCore("metabase", "Metabase", "analytics"),
    AppCore("postiz", "Postiz", "social publishing"),
    AppCore("umami", "Umami", "web analytics"),
    AppCore("listmonk", "Listmonk", "email/lifecycle"),
    AppCore("changedetection", "changedetection.io", "market radar"),
    AppCore("crowdsec", "CrowdSec", "security"),
    AppCore("openscap", "OpenSCAP", "compliance"),
)

APP_CORE_IDS = tuple(app.id for app in APP_CORES)


class OracleVerdict(str, Enum):
    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    CORRECT_HOLD = "CORRECT_HOLD"
    VERIFIED_FAILURE = "VERIFIED_FAILURE"
    UNRESOLVED = "UNRESOLVED"
    UNSUPPORTED = "UNSUPPORTED"


class ScoreSemantics(str, Enum):
    CLASS_PROB = "class_prob"
    RANK_SCORE = "rank_score"
    ORDINAL = "ordinal"
    OUTCOME_PROB = "outcome_prob"
    RUBRIC = "rubric"


def _enum_value(value: object) -> str:
    return value.value if isinstance(value, Enum) else str(value)


@dataclass(frozen=True)
class DecisionModelLane:
    """One bounded decision-model arm under the same evidence/action contract as the LLM/rule arms."""

    provider_id: str
    model_pin: str
    score_semantics: ScoreSemantics | str
    latency_budget_ms: int
    data_classes: tuple[str, ...] = ("synthetic",)
    calibration_pin: str = ""
    cost_ceiling: str = ""
    abstain_allowed: bool = True

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.provider_id:
            errors.append("provider_id is required")
        if not self.model_pin:
            errors.append("model_pin is required")
        try:
            ScoreSemantics(_enum_value(self.score_semantics))
        except ValueError:
            errors.append(f"unknown score_semantics {self.score_semantics!r}")
        if self.latency_budget_ms <= 0:
            errors.append("latency_budget_ms must be positive")
        if not self.data_classes:
            errors.append("at least one allowed data class is required")
        return tuple(errors)

    def is_valid(self) -> bool:
        return not self.validation_errors()


@dataclass(frozen=True)
class AppsE2EScenario:
    """Pinned scenario manifest for a cross-app benchmark case.

    ``app_ids`` names the real cores exercised or explicitly held/unsupported; ``expected_verdicts`` names
    allowed oracle outcomes so a correct hold is not collapsed into a success or failure.
    """

    id: str
    mission_family: str
    app_ids: tuple[str, ...]
    source_dataset: str
    source_digest: str
    expected_verdicts: tuple[OracleVerdict | str, ...]
    required_postconditions: tuple[str, ...] = ()
    forbidden_effects: tuple[str, ...] = ()
    split: str = "holdout"
    as_of: str = ""
    known_at: str = ""
    decision_lane: DecisionModelLane | None = None
    synthetic: bool = True

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.id:
            errors.append("id is required")
        if len(set(self.app_ids)) != len(self.app_ids):
            errors.append("app_ids contains duplicates")
        unknown = sorted(set(self.app_ids) - set(APP_CORE_IDS))
        if unknown:
            errors.append(f"unknown app_ids: {', '.join(unknown)}")
        if not self.source_dataset or not self.source_digest:
            errors.append("source_dataset and source_digest are required")
        if not self.expected_verdicts:
            errors.append("at least one expected oracle verdict is required")
        for verdict in self.expected_verdicts:
            try:
                OracleVerdict(_enum_value(verdict))
            except ValueError:
                errors.append(f"unknown oracle verdict {verdict!r}")
        if self.decision_lane:
            errors.extend(self.decision_lane.validation_errors())
        return tuple(errors)

    def canonical_payload(self) -> Mapping[str, object]:
        lane = asdict(self.decision_lane) if self.decision_lane else None
        if lane:
            lane["score_semantics"] = _enum_value(lane["score_semantics"])
        return {
            "contract_version": CONTRACT_VERSION,
            "id": self.id,
            "mission_family": self.mission_family,
            "app_ids": sorted(self.app_ids),
            "source_dataset": self.source_dataset,
            "source_digest": self.source_digest,
            "expected_verdicts": sorted(_enum_value(v) for v in self.expected_verdicts),
            "required_postconditions": sorted(self.required_postconditions),
            "forbidden_effects": sorted(self.forbidden_effects),
            "split": self.split,
            "as_of": self.as_of,
            "known_at": self.known_at,
            "decision_lane": lane,
            "synthetic": self.synthetic,
        }

    def digest(self) -> str:
        payload = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))
        return content_hash(payload)


@dataclass(frozen=True)
class AppCoverage:
    app_id: str
    scenarios: tuple[str, ...] = ()
    unsupported: bool = False
    note: str = ""


@dataclass(frozen=True)
class CoverageMatrix:
    rows: tuple[AppCoverage, ...]
    missing_required: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing_required


def coverage_matrix(scenarios: Iterable[AppsE2EScenario],
                    unsupported: Mapping[str, str] | None = None) -> CoverageMatrix:
    """Summarize required app-core coverage without inventing capabilities.

    Unsupported apps are kept visible with a note instead of being counted as passing coverage.
    """

    unsupported = unsupported or {}
    seen: dict[str, list[str]] = {app_id: [] for app_id in APP_CORE_IDS}
    for scenario in scenarios:
        errors = scenario.validation_errors()
        if errors:
            raise ValueError("; ".join(errors))
        for app_id in scenario.app_ids:
            seen[app_id].append(scenario.id)
    rows: list[AppCoverage] = []
    missing: list[str] = []
    for app in APP_CORES:
        scenarios_for_app = tuple(sorted(seen[app.id]))
        is_unsupported = app.id in unsupported
        rows.append(AppCoverage(app.id, scenarios_for_app, is_unsupported, unsupported.get(app.id, "")))
        if app.required and not scenarios_for_app and not is_unsupported:
            missing.append(app.id)
    return CoverageMatrix(tuple(rows), tuple(missing))


__all__ = [
    "APP_CORES",
    "APP_CORE_IDS",
    "CONTRACT_VERSION",
    "AppCore",
    "AppCoverage",
    "AppsE2EScenario",
    "CoverageMatrix",
    "DecisionModelLane",
    "OracleVerdict",
    "ScoreSemantics",
    "coverage_matrix",
]
