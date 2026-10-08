"""social-autopilot AppManifest — runtime-native registration contract (plan §3.2/§3.4)."""
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
    return _operator.build_social_operator()


def build_manifest(operator):
    return manifest_from_operator(
        operator,
        name="social-autopilot",
        version="0.1.0",
        producers=(ProducerRef("social_state", opportunity_kinds=("social_opportunity",),
                               emits_capabilities=producers.EMITTED_CAPABILITIES),),
        required_cores=(CoreRequirement("postiz", IntegrationLevel.L2_GOVERNED_FORK),),
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
