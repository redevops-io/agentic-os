"""Derive a baseline AppManifest from a live Operator (fan-out helper, plan §5).

Migrating an app to the runtime-native contract is mostly mechanical: its capabilities and the
verifier each side-effecting one needs are already described by the Operator's ``CapabilitySpec``s.
``manifest_from_operator`` reads them straight from the operator so a per-app ``manifest.py`` only
has to add what the operator cannot express — the decision producers, the cores it wraps, and its
data/privacy profile. This keeps every migrated manifest accurate (capabilities and side-effect
flags come from the real specs, never hand-copied) and the per-app work small.
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence

from agentic_os.agent_gateway.contracts import RiskTier

from .manifest import (
    AppManifest,
    CapabilityRef,
    CoreRequirement,
    DeploySpec,
    EnterpriseRequirement,
    OutcomeKind,
    PrivacyProfile,
    ProducerRef,
    VerifierRef,
)


def _verifier_target(spec) -> str:
    """A label for the composite read-back verifier: the capability's first declared output, else its
    first ``provides`` outcome, else the capability name."""
    outputs = getattr(spec, "outputs", {}) or {}
    for key in outputs:
        return f"composite:{key}"
    provides = getattr(spec, "provides", []) or []
    if provides:
        return f"composite:{provides[0]}"
    return f"composite:{spec.name}"


def manifest_from_operator(
    operator,
    *,
    name: Optional[str] = None,
    version: str = "0.1.0",
    producers: Sequence[ProducerRef] = (),
    outcome_kinds: Sequence[OutcomeKind] = (),
    obligation_kinds: Sequence[str] = (),
    required_cores: Sequence[CoreRequirement] = (),
    privacy: Optional[PrivacyProfile] = None,
    data_classifications: Sequence = (),
    enterprise: EnterpriseRequirement = EnterpriseRequirement.OPTIONAL,
    risk_tiers: Optional[Mapping[str, RiskTier]] = None,
    deploy: Optional[DeploySpec] = None,
) -> AppManifest:
    """Build an ``AppManifest`` whose capabilities and verifiers come from ``operator``'s specs.

    Each capability becomes a ``CapabilityRef`` (risk tier from ``risk_tiers`` if given, else
    BOUNDED_WRITE for a side-effecting capability and READ otherwise). Each *side-effecting*
    capability gets a ``VerifierRef`` (the runtime's composite read-back of its declared output),
    satisfying N4 automatically. Everything else is passed through.
    """
    specs = list(operator.manifest.capabilities)
    tiers = dict(risk_tiers or {})

    def tier_for(spec) -> RiskTier:
        if spec.name in tiers:
            return tiers[spec.name]
        return RiskTier.BOUNDED_WRITE if getattr(spec, "side_effecting", False) else RiskTier.READ

    capabilities = tuple(CapabilityRef(s.name, risk_tier=tier_for(s)) for s in specs)
    verifiers = tuple(
        VerifierRef(s.name, _verifier_target(s)) for s in specs if getattr(s, "side_effecting", False)
    )
    return AppManifest(
        name=name or operator.name,
        version=version,
        producers=tuple(producers),
        capabilities=capabilities,
        verifiers=verifiers,
        outcome_kinds=tuple(outcome_kinds),
        obligation_kinds=tuple(obligation_kinds),
        privacy=privacy or PrivacyProfile(),
        required_cores=tuple(required_cores),
        data_classifications=tuple(data_classifications),
        enterprise=enterprise,
        deploy=deploy,
    )


__all__ = ["manifest_from_operator"]
