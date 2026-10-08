"""agentic-privacy AppManifest — runtime-native registration contract (plan §3.2/§3.4)."""
from __future__ import annotations

from agentic_os.app_kit import DeploySpec

from agentic_os.app_kit import PrivacyProfile, ProducerRef, manifest_from_operator
from agentic_os.governance.classification import DataClassification
from agentic_os.governance.routing import ExecutionBoundary

from . import operator as _operator
from . import producers


def default_operator():
    return _operator.build_privacy_operator()


def build_manifest(operator):
    return manifest_from_operator(
        operator,
        name="agentic-privacy",
        version="0.1.0",
        producers=(ProducerRef("privacy_state", opportunity_kinds=("privacy_opportunity",),
                               emits_capabilities=producers.EMITTED_CAPABILITIES),),
        privacy=PrivacyProfile(
            data_classes=(DataClassification.CUSTOMER_RESTRICTED,),
            execution_boundary=ExecutionBoundary.IN_BOUNDARY,
        ),
        data_classifications=(DataClassification.CUSTOMER_RESTRICTED,),
        deploy=DeploySpec(
            port=8212,
            pain='GDPR/CCPA data-subject requests',
            tagline='DSAR intake, fulfillment, and a tamper-evident audit trail.',
            agents=('intake', 'access', 'delete', 'retention'),
            approval=('delete',),
            deploy='compose',
        ),
    )


MANIFEST = build_manifest(default_operator())


def register(registry, operator=None):
    op = operator or default_operator()
    return registry.register(build_manifest(op), op)


__all__ = ["MANIFEST", "build_manifest", "default_operator", "register"]
