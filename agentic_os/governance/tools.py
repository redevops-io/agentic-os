"""Tool security profiles + planner eligibility (Private Data Plane plan §8).

Every tool declares what data classes it may accept, its network boundary, whether it hands data to an external
processor, its credential scope and whether it writes external state / logs payloads. Eligibility = evidence
classification + tool profile + execution context — a tool that would send private data to an external processor is
ineligible, fail-closed. Pure.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .classification import DataClassification, externally_shareable
from .routing import ExecutionBoundary


@dataclass(frozen=True)
class ToolSecurityProfile:
    tool_id: str
    data_classes_accepted: DataClassification = DataClassification.SECRET   # highest it may receive
    network_boundary: ExecutionBoundary = ExecutionBoundary.IN_BOUNDARY
    external_data_processor: bool = False
    credential_scope: str = ""
    writes_external_state: bool = False
    logs_payloads: bool = False


def tool_eligible(profile: ToolSecurityProfile, classification: DataClassification) -> bool:
    """Deterministic eligibility for a given evidence classification."""
    if classification.rank > profile.data_classes_accepted.rank:
        return False
    # a tool that sends data to an external processor / lives outside the boundary may only touch shareable data
    if (profile.external_data_processor or profile.network_boundary is ExecutionBoundary.EXTERNAL) \
            and not externally_shareable(classification):
        return False
    # a tool that logs payloads may not handle private data (observability must not leak payloads, §15)
    if profile.logs_payloads and not externally_shareable(classification):
        return False
    return True


def ineligibility_reason(profile: ToolSecurityProfile, classification: DataClassification) -> str:
    if classification.rank > profile.data_classes_accepted.rank:
        return f"{profile.tool_id} accepts <= {profile.data_classes_accepted.value}, got {classification.value}"
    if (profile.external_data_processor or profile.network_boundary is ExecutionBoundary.EXTERNAL) \
            and not externally_shareable(classification):
        return f"{profile.tool_id} would send {classification.value} to an external processor"
    if profile.logs_payloads and not externally_shareable(classification):
        return f"{profile.tool_id} logs payloads; {classification.value} must not be logged"
    return ""


__all__ = ["ToolSecurityProfile", "tool_eligible", "ineligibility_reason"]
