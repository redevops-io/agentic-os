"""Private Data Plane governance — data classification, fail-closed model routing, tool profiles, receipts.

The commercial capability contracts are unchanged; this package constrains WHERE their evidence/workers/models/tools
may execute. Core invariant: private business data stays inside the trust boundary; external frontier models serve
only ENGINEERING/PUBLIC context; no compliant route → fail closed. Pure contracts + deterministic policy.
"""
from .classification import DataClassification, EXTERNAL_MAX, externally_shareable, max_classification
from .routing import (
    ExecutionBoundary, GovernedModelRouter, ModelEndpoint, ModelRequest, RoutingDecision, RoutingRefused, TaskClass,
)
from .tools import ToolSecurityProfile, ineligibility_reason, tool_eligible
from .receipt import InferenceReceipt, private_records_to_external, receipt_for

__all__ = [
    "DataClassification", "EXTERNAL_MAX", "max_classification", "externally_shareable",
    "ExecutionBoundary", "TaskClass", "GovernedModelRouter", "ModelEndpoint", "ModelRequest",
    "RoutingDecision", "RoutingRefused",
    "ToolSecurityProfile", "tool_eligible", "ineligibility_reason",
    "InferenceReceipt", "receipt_for", "private_records_to_external",
]
