"""growth-engine AppManifest — runtime-native registration contract (plan §3.2).

Registers growth-engine's governed capability surface with the App Kit: its operator capabilities, a
read-back verifier for each side-effecting one (N4, derived from the specs), the core it wraps at
its integration level, and its data/privacy profile. ``register`` fails closed on any conformance
violation. Decision-spine producer wiring (N1) is a follow-up slice where a producer maps cleanly.
"""
from __future__ import annotations

from agentic_os.app_kit import (
    CoreRequirement,
    IntegrationLevel,
    PrivacyProfile,
    manifest_from_operator,
)
from agentic_os.governance.classification import DataClassification
from agentic_os.governance.routing import ExecutionBoundary

from . import operator as _operator


def default_operator():
    return _operator.build_growth_operator()


def build_manifest(operator):
    return manifest_from_operator(
        operator,
        name="growth-engine",
        version="0.1.0",
        required_cores=(CoreRequirement("umami", IntegrationLevel.L0_ADAPTER),),
        privacy=PrivacyProfile(
            data_classes=(DataClassification.CUSTOMER_CONFIDENTIAL,),
            execution_boundary=ExecutionBoundary.IN_BOUNDARY,
        ),
        data_classifications=(DataClassification.CUSTOMER_CONFIDENTIAL,),
    )


MANIFEST = build_manifest(default_operator())


def register(registry, operator=None):
    op = operator or default_operator()
    return registry.register(build_manifest(op), op)


__all__ = ["MANIFEST", "build_manifest", "default_operator", "register"]
