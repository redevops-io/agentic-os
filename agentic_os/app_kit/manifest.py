"""AppManifest — the registration contract for a runtime-native agentic app.

An app becomes runtime-native (see the Runtime-Native Redesign plan, N1-N11) by declaring an
``AppManifest`` and registering it with an :class:`~agentic_os.app_kit.registry.AppRegistry`
alongside its Mission :class:`~agentic_os.mission.operator_sdk.Operator`. The manifest is the
*app-level* contract: which decision producers it runs, which governed capabilities it provides,
which capability each verifier reads back, what outcomes it emits, which cores it needs and at
what integration level, and its data/privacy profile.

Per-capability security flags (``side_effecting``, ``approval_required``, ``data_classifications``)
are NOT duplicated here — they live on the Operator's ``CapabilitySpec`` and the registry reads
them from there. The manifest only *names* the capabilities the app provides and adds the
runtime-native metadata the Operator cannot express (producers, verifiers, outcomes, cores).

Pure data: no imports from the mission runtime, so a manifest can be declared and inspected with
no runtime wired. The registry (which does bind to the Operator) enforces the invariants.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Tuple

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.governance.classification import DataClassification
from agentic_os.governance.policy import PrivacyMode
from agentic_os.governance.routing import ExecutionBoundary

MANIFEST_CONTRACT_VERSION = "app-manifest/v1"


class IntegrationLevel(str, Enum):
    """How an app integrates with the OSS core it wraps (plan §4.1)."""

    L0_ADAPTER = "l0_adapter"            # REST/CLI client + polling
    L1_EXTENSION = "l1_extension"        # the core's own webhooks/plugins; native-UI actions observed
    L2_GOVERNED_FORK = "l2_governed_fork"  # outbox + envelope guard + read-back
    L3_EMBEDDED = "l3_embedded"          # runtime runs in-process inside the core


class EnterpriseRequirement(str, Enum):
    """Whether the app needs a private enterprise image to meet its contract (plan §3.8)."""

    OPTIONAL = "optional"   # public seam defaults suffice for N1-N10; enterprise only adds capability
    REQUIRED = "required"   # enterprise-only suite; never published as an AGPL app


@dataclass(frozen=True)
class PrivacyProfile:
    """The data/egress posture of an app or capability, aligned with ``governance.tools``'
    ``ToolSecurityProfile``. The highest data class the surface may receive is the max of
    ``data_classes``; everything else constrains egress."""

    data_classes: Tuple[DataClassification, ...] = (DataClassification.PUBLIC,)
    execution_boundary: ExecutionBoundary = ExecutionBoundary.IN_BOUNDARY
    privacy_modes: Tuple[PrivacyMode, ...] = ()   # supported modes; () ⇒ no restriction
    approved_egress: Tuple[str, ...] = ()         # hostnames/endpoints egress is allowed to
    external_processors: Tuple[str, ...] = ()     # named third-party data processors it may reach
    credential_scope: str = ""
    logs_payloads: bool = False


@dataclass(frozen=True)
class CapabilityRef:
    """An app's claim that it provides capability ``name`` as part of the runtime-native contract.

    ``name`` must resolve to a capability registered on the app's Operator. ``risk_tier`` is the
    one risk vocabulary (plan §3.3) — carried here until ``CapabilitySpec`` grows the field. An
    optional ``privacy`` override narrows the app-level profile for this one capability.
    """

    name: str
    risk_tier: RiskTier = RiskTier.READ
    privacy: Optional[PrivacyProfile] = None


@dataclass(frozen=True)
class ProducerRef:
    """A decision producer the app runs: domain state -> Opportunity/InterventionCandidate.

    ``emits_capabilities`` are the capability names the producer's candidates may require
    (``InterventionCandidate.required_capabilities``). The registry checks every one resolves —
    this is what kills phantom capabilities (plan §3.4)."""

    name: str
    opportunity_kinds: Tuple[str, ...] = ()
    emits_capabilities: Tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifierRef:
    """A read-back verifier bound to a side-effecting capability (N4). ``capability`` must resolve;
    ``name`` identifies the verifier / connector ``observe`` reference."""

    capability: str
    name: str = ""


@dataclass(frozen=True)
class OutcomeKind:
    """An outcome the app emits. ``key`` is usually the candidate's ``action_kind``. ``expected_delay_s``
    is in seconds (the plan mandates seconds everywhere)."""

    key: str
    unit: str = ""
    expected_delay_s: float = 0.0
    reward_weights: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CoreRequirement:
    """An OSS core the app depends on, and the integration level it is reached at (plan §4)."""

    core: str
    integration_level: IntegrationLevel = IntegrationLevel.L0_ADAPTER


@dataclass(frozen=True)
class DeploySpec:
    """The catalog/deploy metadata the app owns so ``modules.yaml`` can be GENERATED from the
    manifests (plan §3.2). ``catalog_name`` is the module's display name when it differs from the app
    name; the generated ``source`` is always ``apps/<app-name>`` (fixing the historical path drift)."""

    catalog_name: str = ""          # modules.yaml display name if != manifest.name
    port: Optional[int] = None      # published host/service port (None for operator/tool-only apps)
    pain: str = ""                  # the customer pain the app addresses
    tagline: str = ""
    agents: Tuple[str, ...] = ()    # the catalog's human-facing agent roles
    approval: Tuple[str, ...] = ()  # the catalog's approval-gated action labels
    deploy: str = "compose"         # compose | operator | tool


@dataclass(frozen=True)
class AppManifest:
    """The runtime-native registration contract for one app."""

    name: str
    version: str = "0.0.0"
    license: str = "AGPL-3.0-or-later"
    producers: Tuple[ProducerRef, ...] = ()
    capabilities: Tuple[CapabilityRef, ...] = ()
    verifiers: Tuple[VerifierRef, ...] = ()
    outcome_kinds: Tuple[OutcomeKind, ...] = ()
    obligation_kinds: Tuple[str, ...] = ()
    privacy: PrivacyProfile = PrivacyProfile()
    required_cores: Tuple[CoreRequirement, ...] = ()
    data_classifications: Tuple[DataClassification, ...] = ()
    enterprise: EnterpriseRequirement = EnterpriseRequirement.OPTIONAL
    deploy: Optional["DeploySpec"] = None
    contract_version: str = MANIFEST_CONTRACT_VERSION

    def capability_names(self) -> frozenset[str]:
        return frozenset(c.name for c in self.capabilities)

    def producer_capabilities(self) -> frozenset[str]:
        """Every capability any producer's candidates may require."""
        return frozenset(cap for p in self.producers for cap in p.emits_capabilities)

    def verified_capabilities(self) -> frozenset[str]:
        return frozenset(v.capability for v in self.verifiers)


__all__ = [
    "MANIFEST_CONTRACT_VERSION",
    "IntegrationLevel",
    "EnterpriseRequirement",
    "PrivacyProfile",
    "CapabilityRef",
    "ProducerRef",
    "VerifierRef",
    "OutcomeKind",
    "CoreRequirement",
    "DeploySpec",
    "AppManifest",
]
