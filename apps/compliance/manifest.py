"""compliance AppManifest — runtime-native registration contract (plan §3.2).

Registers compliance's governed capability surface with the App Kit (capabilities + a read-back verifier
per side-effecting capability, derived from the operator specs; data/privacy profile; wraps openscap at L0_ADAPTER).
``register`` fails closed on any conformance violation. Decision-spine producer wiring (N1) follows.
"""
from __future__ import annotations

from agentic_os.app_kit import PrivacyProfile, manifest_from_operator
from agentic_os.governance.classification import DataClassification
from agentic_os.governance.routing import ExecutionBoundary
from agentic_os.app_kit import CoreRequirement, IntegrationLevel

from . import operator as _operator


def default_operator():
    return _operator.build_compliance_operator()


def build_manifest(operator):
    return manifest_from_operator(
        operator,
        name="compliance",
        version="0.1.0",
        required_cores=(CoreRequirement("openscap", IntegrationLevel.L0_ADAPTER),),
        privacy=PrivacyProfile(
            data_classes=(DataClassification.INTERNAL,),
            execution_boundary=ExecutionBoundary.IN_BOUNDARY,
        ),
        data_classifications=(DataClassification.INTERNAL,),
    )


MANIFEST = build_manifest(default_operator())


def register(registry, operator=None):
    op = operator or default_operator()
    return registry.register(build_manifest(op), op)


__all__ = ["MANIFEST", "build_manifest", "default_operator", "register"]
