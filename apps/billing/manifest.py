"""billing AppManifest — runtime-native registration contract (plan §3.2/§3.4).

Registers billing's governed capability surface (capabilities + a read-back verifier per side-effecting
capability, from the operator specs), its decision producer (see ``producers``), the core it wraps at
its integration level, and its data/privacy profile. ``register`` fails closed on any violation.
"""
from __future__ import annotations

from agentic_os.app_kit import (
    CoreRequirement,
    IntegrationLevel,
    PrivacyProfile,
    ProducerRef,
    manifest_from_operator,
)
from agentic_os.governance.classification import DataClassification
from agentic_os.governance.routing import ExecutionBoundary

from . import operator as _operator
from . import producers


def default_operator():
    return _operator.build_billing_operator()


def build_manifest(operator):
    return manifest_from_operator(
        operator,
        name="billing",
        version="0.1.0",
        producers=(ProducerRef("billing_signals", opportunity_kinds=("billing_opportunity",),
                               emits_capabilities=producers.EMITTED_CAPABILITIES),),
        required_cores=(CoreRequirement("lago", IntegrationLevel.L2_GOVERNED_FORK),),
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
