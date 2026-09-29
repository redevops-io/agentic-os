"""Membrane as ExecutionBackend #1 (agent-security seam, runtime-contracts #24).

The rootless :class:`LocalContainmentSandbox` is the reference executor; this adapts it to the provider-neutral
``ExecutionBackend`` Protocol and — crucially — advertises the capabilities it *actually* enforces, so the
capability-negotiated admission gate can refuse a profile the membrane cannot honor instead of running it anyway.

Honest capabilities: the rootless membrane enforces filesystem containment (contained cwd + constrained paths),
resource ceilings, process isolation, and envelope/identity binding. It does NOT enforce network-egress control
(that is the enterprise net-namespace wrapper), does not do live quarantine, and produces no host/hardware
attestation ("empty is honest for a trusted local membrane"). It therefore satisfies STANDARD; an ISOLATED+ mission
(which requires network-egress control) fails closed here and must route to the enterprise sandbox / OpenShell.
"""
from __future__ import annotations

import time
from typing import Optional

from runtime_contracts.models import (
    BackendCapabilities, EnforcementProfile, ExecutionAdmission, ExecutionEnvelope, ExecutionReceipt,
    admit_backend, profile_requirements,
)
from runtime_contracts.protocol import ExecutionIdentity

from .membrane import ContainmentError, LocalContainmentSandbox

# What the rootless reference membrane can truthfully enforce.
MEMBRANE_CAPABILITIES = BackendCapabilities(
    filesystem_isolation=True,
    resource_limits=True,
    process_isolation=True,
    identity_binding=True,
    # network_egress_control / quarantine / attestation / hardware_attestation: NOT enforced rootless.
)


class MembraneExecutionBackend:
    """``ExecutionBackend`` #1 wrapping :class:`LocalContainmentSandbox`."""
    backend_id = "membrane.local"

    def __init__(self, sandbox: Optional[LocalContainmentSandbox] = None):
        self._sandbox = sandbox or LocalContainmentSandbox()

    def capabilities(self) -> BackendCapabilities:
        return MEMBRANE_CAPABILITIES

    def execute(self, envelope: ExecutionEnvelope, identity: ExecutionIdentity,
                profile: EnforcementProfile) -> ExecutionReceipt:
        """Run one approved envelope in the membrane. Defensive: even if a caller skipped the admission gate, a
        backend never runs above the tier it can enforce — an unmet profile is a first-class refusal receipt."""
        if not self.capabilities().satisfies(profile_requirements(profile)):
            return self._refusal(envelope, "enforcement_downgrade")
        started = time.time()
        try:
            result = self._sandbox.run_contained(
                self.backend_id, envelope.capability_id, dict(envelope.parameters),
                envelope.idempotency_key, constraint=envelope.constraint)
        except ContainmentError as e:
            return self._refusal(envelope, str(e))
        # Success receipt is bound to the envelope; identity stays linkable via envelope.authority.
        return self._sandbox.receipt(envelope, result, started=started, finished=time.time())

    def _refusal(self, envelope: ExecutionEnvelope, reason: str) -> ExecutionReceipt:
        return ExecutionReceipt(
            envelope_binding=envelope.binding, mission_id=envelope.mission_id,
            capability_id=envelope.capability_id, idempotency_key=envelope.idempotency_key,
            outcome="refused", reason=reason)

    def terminate(self, execution_id: str) -> None:
        # The membrane runs one contained, bounded child per call; there is no long-lived execution to terminate.
        return None

    def status(self, execution_id: str) -> str:
        return "completed"

    def attest(self, execution_id: str) -> str:
        # A trusted local membrane produces no out-of-band attestation; honest empty.
        return ""


def govern_and_execute(backend, envelope: ExecutionEnvelope, identity: ExecutionIdentity,
                       profile: EnforcementProfile, *, allow_downgrade: bool = False
                       ) -> "tuple[ExecutionAdmission, ExecutionReceipt]":
    """The gated flow: admit the backend for the required profile FIRST (fail-closed on a downgrade), then execute.
    Returns the audit admission + the receipt. Credentials should be admitted between admission and execution."""
    admission = admit_backend(profile, backend, envelope, identity, allow_downgrade=allow_downgrade)
    receipt = backend.execute(envelope, identity, profile)
    return admission, receipt
