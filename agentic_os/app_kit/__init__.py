"""App Kit — the runtime-native app contract (plan §2.1/§3).

An agentic app becomes runtime-native by declaring an :class:`AppManifest` (producers,
capabilities, verifiers, outcome kinds, required cores, privacy profile) and registering it with
an :class:`AppRegistry` alongside its Mission ``Operator``. The registry enforces the static
runtime-native invariants at registration time; the dynamic suite runs in ``tests/conformance/``.
"""
from __future__ import annotations

from .conformance import Finding, blocking_findings, check_registration, summarize
from .manifest import (
    MANIFEST_CONTRACT_VERSION,
    AppManifest,
    CapabilityRef,
    CoreRequirement,
    EnterpriseRequirement,
    IntegrationLevel,
    OutcomeKind,
    PrivacyProfile,
    ProducerRef,
    VerifierRef,
)
from .registry import AppRegistry, ManifestError, RegisteredApp

__all__ = [
    "MANIFEST_CONTRACT_VERSION",
    "AppManifest",
    "CapabilityRef",
    "ProducerRef",
    "VerifierRef",
    "OutcomeKind",
    "CoreRequirement",
    "IntegrationLevel",
    "EnterpriseRequirement",
    "PrivacyProfile",
    "AppRegistry",
    "RegisteredApp",
    "ManifestError",
    "Finding",
    "check_registration",
    "blocking_findings",
    "summarize",
]
