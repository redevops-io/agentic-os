"""document-editor AppManifest: runtime-native registration contract.

Registers the governed ``document.*`` capability surface and a read-back verifier per side-effecting capability
(derived from the operator specs). ``register`` fails closed on any violation.
"""
from __future__ import annotations

from agentic_os.app_kit import (CoreRequirement, DeploySpec, IntegrationLevel, PrivacyProfile, manifest_from_operator)
from agentic_os.governance.classification import DataClassification
from agentic_os.governance.routing import ExecutionBoundary

from . import operator as _operator


def default_operator():
    # The manifest only needs the capability SPECS: handlers are never called at registration, and the default
    # provider builds the service lazily (so a missing configuration cannot break registration).
    return _operator.build_document_editor_operator()


def build_manifest(operator):
    return manifest_from_operator(
        operator,
        name="document-editor",
        version="0.1.0",
        required_cores=(CoreRequirement("onlyoffice-documentserver", IntegrationLevel.L0_ADAPTER),),
        privacy=PrivacyProfile(
            data_classes=(DataClassification.CUSTOMER_CONFIDENTIAL,),
            execution_boundary=ExecutionBoundary.IN_BOUNDARY,
        ),
        data_classifications=(DataClassification.CUSTOMER_CONFIDENTIAL,),
        deploy=DeploySpec(
            catalog_name="agentic-document-editor",
            pain="governed document editing & conversion (operator)",
            tagline="Convert, review and edit documents through your own editor, with approval before any write.",
            agents=("converter", "reviewer", "editor"),
            approval=("apply_patch", "publish", "set_label"),
            deploy="operator",
        ),
    )


MANIFEST = build_manifest(default_operator())


def register(registry, operator=None):
    op = operator or default_operator()
    return registry.register(build_manifest(op), op)


__all__ = ["MANIFEST", "build_manifest", "default_operator", "register"]
