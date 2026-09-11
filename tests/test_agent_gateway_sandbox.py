"""Action sandbox — Phase 6, on the canonical containment seam (plan §8).

The gateway's ``sandbox.execute`` routes to an ``agentic_os.mission.executor.Sandbox`` — the SAME
contract the mission plane and the Enterprise SubprocessSandbox use — not a parallel sandbox. Tested
two ways: the gateway wiring against a fake Sandbox, and a real run through the open
LocalContainmentSandbox (which executes in a confined child, proving the containment path).
"""
from __future__ import annotations

from agentic_os.overlays import Principal
from agentic_os.agent_gateway import (
    AgentGateway, GatewayPrincipal, GatewayRequest, GatewayStatus, register_sandbox_capability)
from agentic_os.agent_gateway.registry import CapabilityRegistry


class _FakeSandbox:
    """Satisfies executor.Sandbox by shape: invoke(operator, capability, inputs, key, *, isolation)."""
    def __init__(self): self.calls = []
    def invoke(self, operator, capability, inputs, idempotency_key, *, isolation="sandbox", grants=None):
        self.calls.append({"operator": operator, "capability": capability, "inputs": inputs,
                           "isolation": isolation})
        return {"ran": capability, "inputs": inputs, "isolation": isolation}


class _Approve:
    def is_satisfied(self, request, manifest): return True


def _gp():
    return GatewayPrincipal(Principal("agent", "service", (), "acme"))


def _gw(sandbox, *, approvals=None):
    reg = register_sandbox_capability(CapabilityRegistry(), sandbox)
    kw = {"registry": reg, "authorize": lambda p, perm: perm == "sandbox.execute"}
    if approvals is not None:                         # else keep the gateway's deny-by-default store
        kw["approvals"] = approvals
    return AgentGateway(**kw)


# ── the capability is the strictest gate ──────────────────────────────────────────
def test_sandbox_execute_is_critical_and_mandatory_approval():
    from agentic_os.agent_gateway.sandbox import SANDBOX_EXECUTE
    from agentic_os.agent_gateway import RiskTier, ApprovalPolicy
    assert SANDBOX_EXECUTE.risk_tier is RiskTier.CRITICAL
    assert SANDBOX_EXECUTE.effective_approval_policy is ApprovalPolicy.MANDATORY


def test_execute_gates_on_approval_before_running():
    fake = _FakeSandbox()
    gw = _gw(fake)                                    # no approvals
    r = gw.invoke(GatewayRequest(_gp(), "sandbox.execute",
                                 {"capability": "pkg.mod:fn", "inputs": {"a": 1}}))
    assert r.status is GatewayStatus.PENDING_APPROVAL and fake.calls == []   # never ran


# ── routes through the canonical Sandbox.invoke ────────────────────────────────────
def test_execute_routes_to_the_sandbox_invoke_contract():
    fake = _FakeSandbox()
    gw = _gw(fake, approvals=_Approve())
    r = gw.invoke(GatewayRequest(_gp(), "sandbox.execute",
                                 {"capability": "pkg.mod:fn", "inputs": {"a": 1}, "isolation": "strict"}))
    assert r.status is GatewayStatus.OK
    assert fake.calls and fake.calls[0]["capability"] == "pkg.mod:fn"
    assert fake.calls[0]["inputs"] == {"a": 1} and fake.calls[0]["isolation"] == "strict"


def test_execute_requires_a_module_attr_target():
    fake = _FakeSandbox()
    gw = _gw(fake, approvals=_Approve())
    r = gw.invoke(GatewayRequest(_gp(), "sandbox.execute", {"capability": "not-a-target"}))
    assert r.status is GatewayStatus.ERROR and "module:attr" in r.error
    assert fake.calls == []


def test_a_containment_failure_is_a_clean_error_not_a_crash():
    class _Boom:
        def invoke(self, *a, **k): raise RuntimeError("contained_failure(rc=137)")
    gw = _gw(_Boom(), approvals=_Approve())
    r = gw.invoke(GatewayRequest(_gp(), "sandbox.execute", {"capability": "pkg.mod:fn"}))
    assert r.status is GatewayStatus.ERROR and "contained_failure" in r.error


# ── a real run through the open LocalContainmentSandbox (default backend) ──────────
def test_default_backend_runs_a_capability_in_real_containment():
    # default sandbox = LocalContainmentSandbox; the target executes in a confined child (scrubbed
    # env, rlimits), proving the gateway shares the runtime's actual containment path.
    reg = register_sandbox_capability(CapabilityRegistry())      # no sandbox ⇒ the open default
    gw = AgentGateway(registry=reg, authorize=lambda p, perm: perm == "sandbox.execute",
                      approvals=_Approve())
    r = gw.invoke(GatewayRequest(_gp(), "sandbox.execute",
                                 {"capability": "agentic_os.agent_gateway.sandbox:echo_capability",
                                  "inputs": {"x": 1}}))
    assert r.status is GatewayStatus.OK and r.output == {"echo": {"x": 1}}
