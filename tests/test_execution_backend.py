"""MembraneExecutionBackend — the reference membrane as ExecutionBackend #1 (runtime-contracts #24 seam).

Proves honest capability advertisement, fail-closed profile admission (the membrane cannot be tricked into running
above the tier it enforces), real contained execution, and identity-bound receipts/admissions.
"""
from __future__ import annotations

import pytest

from runtime_contracts.models import (
    BackendCapabilities, EnforcementDowngrade, EnforcementProfile, ExecutionConstraint, ExecutionEnvelope,
    ExecutionBackend, admit_backend, profile_requirements,
)
from runtime_contracts.models.capability import Idempotency
from runtime_contracts.protocol import AuthorityContext, ExecutionIdentity, PrincipalRef

from agentic_os.mission.execution_backend import (
    MEMBRANE_CAPABILITIES, MembraneExecutionBackend, govern_and_execute,
)

CAP = "agentic_os.mission._membrane_selftest"


def _identity():
    auth = AuthorityContext(authority_id="a0", principal=PrincipalRef(id="agent:planner", kind="agent", tenant="t1"),
                            purpose="mission", scope=("run",))
    return ExecutionIdentity.of(auth, mission_id="m-1")


def _envelope(cap=f"{CAP}:echo", params=None):
    return ExecutionEnvelope(
        mission_id="m-1", plan_fingerprint="rcv1:p", capability_id=cap, authority="g7", target="local",
        parameters=params or {"x": 1},
        constraint=ExecutionConstraint(max_memory_mb=512, max_duration_seconds=5, max_processes=64),
        idempotency=Idempotency.AT_MOST_ONCE, idempotency_key="idem-1", not_after="2999-01-01T00:00:00Z")


def test_backend_conforms_to_protocol():
    assert isinstance(MembraneExecutionBackend(), ExecutionBackend)
    assert MembraneExecutionBackend().backend_id == "membrane.local"


def test_capabilities_are_honest():
    caps = MembraneExecutionBackend().capabilities()
    assert caps == MEMBRANE_CAPABILITIES
    # satisfies STANDARD, but not ISOLATED (rootless membrane can't control network egress)
    assert caps.satisfies(profile_requirements(EnforcementProfile.STANDARD))
    assert not caps.satisfies(profile_requirements(EnforcementProfile.ISOLATED))
    assert "network_egress_control" in caps.missing(profile_requirements(EnforcementProfile.ISOLATED))


def test_admission_fails_closed_above_membrane_tier():
    be, env, ident = MembraneExecutionBackend(), _envelope(), _identity()
    assert admit_backend(EnforcementProfile.STANDARD, be, env, ident).granted
    with pytest.raises(EnforcementDowngrade):
        admit_backend(EnforcementProfile.ISOLATED, be, env, ident)
    with pytest.raises(EnforcementDowngrade):
        admit_backend(EnforcementProfile.HARDWARE_ATTESTED, be, env, ident)


def test_execute_runs_contained_capability():
    be, env, ident = MembraneExecutionBackend(), _envelope(), _identity()
    r = be.execute(env, ident, EnforcementProfile.STANDARD)
    assert r.outcome == "executed" and r.envelope_binding == env.binding
    assert r.side_effect_digest.startswith("rcv1:")


def test_execute_refuses_profile_it_cannot_enforce():
    be, env, ident = MembraneExecutionBackend(), _envelope(), _identity()
    # even bypassing the gate, the backend never runs above its tier
    r = be.execute(env, ident, EnforcementProfile.ISOLATED)
    assert r.outcome == "refused" and r.reason == "enforcement_downgrade"


def test_govern_and_execute_binds_identity_and_gates():
    be, env, ident = MembraneExecutionBackend(), _envelope(), _identity()
    admission, receipt = govern_and_execute(be, env, ident, EnforcementProfile.STANDARD)
    assert admission.granted and admission.execution_identity == ident.digest()
    assert admission.envelope_binding == env.binding == receipt.envelope_binding   # deterministic linkage
    assert receipt.outcome == "executed"
    # the gated flow fails closed BEFORE executing when the profile exceeds the backend
    with pytest.raises(EnforcementDowngrade):
        govern_and_execute(be, env, ident, EnforcementProfile.HARDENED)


def test_execute_containment_failure_is_a_refusal_receipt():
    be, ident = MembraneExecutionBackend(), _identity()
    env = _envelope(cap=f"{CAP}:busy",
                    params={})  # a capability that blows its resource ceiling
    tight = ExecutionEnvelope(
        mission_id="m-1", plan_fingerprint="rcv1:p", capability_id=f"{CAP}:alloc", authority="g7", target="local",
        parameters={"mb": 8192}, constraint=ExecutionConstraint(max_memory_mb=64, max_duration_seconds=3, max_processes=64),
        idempotency=Idempotency.AT_MOST_ONCE, idempotency_key="idem-2", not_after="2999-01-01T00:00:00Z")
    r = be.execute(tight, ident, EnforcementProfile.STANDARD)
    assert r.outcome == "refused" and r.reason   # named containment reason, not a crash
