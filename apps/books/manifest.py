"""books AppManifest — runtime-native registration contract (plan §3.2/§3.4).

Registers books's governed capability surface (capabilities + a read-back verifier per side-effecting
capability, from the operator specs), its decision producer (see ``producers``), the core it wraps at
its integration level, and its data/privacy profile. ``register`` fails closed on any violation.
"""
from __future__ import annotations

from agentic_os.app_kit import DeploySpec

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
    return _operator.build_books_operator()


def build_manifest(operator):
    return manifest_from_operator(
        operator,
        name="books",
        version="0.1.0",
        producers=(ProducerRef("books_state", opportunity_kinds=("books_opportunity",),
                               emits_capabilities=producers.EMITTED_CAPABILITIES),),
        required_cores=(CoreRequirement("erpnext", IntegrationLevel.L1_EXTENSION),),
        privacy=PrivacyProfile(
            data_classes=(DataClassification.CUSTOMER_CONFIDENTIAL,),
            execution_boundary=ExecutionBoundary.IN_BOUNDARY,
        ),
        data_classifications=(DataClassification.CUSTOMER_CONFIDENTIAL,),
        deploy=DeploySpec(
            catalog_name='agentic-books',
            port=8209,
            pain='bookkeeping & close',
            tagline='Books that categorize, reconcile, and close themselves.',
            agents=('categorize', 'reconcile', 'close'),
            approval=('close',),
            deploy='compose',
        ),
    )


MANIFEST = build_manifest(default_operator())


def register(registry, operator=None):
    op = operator or default_operator()
    return registry.register(build_manifest(op), op)


__all__ = ["MANIFEST", "build_manifest", "default_operator", "register"]
