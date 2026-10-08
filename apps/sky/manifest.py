"""sky AppManifest — runtime-native registration contract (plan §3.2/§3.4)."""
from __future__ import annotations

from agentic_os.app_kit import PrivacyProfile, ProducerRef, manifest_from_operator
from agentic_os.governance.classification import DataClassification
from agentic_os.governance.routing import ExecutionBoundary
from agentic_os.app_kit import CoreRequirement, IntegrationLevel

from . import operator as _operator
from . import producers


def default_operator():
    return _operator.build_sky_operator()


def build_manifest(operator):
    return manifest_from_operator(
        operator,
        name="sky",
        version="0.1.0",
        producers=(ProducerRef("sky_state", opportunity_kinds=("sky_opportunity",),
                               emits_capabilities=producers.EMITTED_CAPABILITIES),),
        required_cores=(CoreRequirement("skypilot", IntegrationLevel.L0_ADAPTER),),
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
