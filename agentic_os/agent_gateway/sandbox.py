"""Phase 6 — the action sandbox capability, on the canonical AGPL containment seam.

Consistency (open ↔ enterprise): this does **not** define its own sandbox. It exposes the existing
:class:`agentic_os.mission.executor.Sandbox` contract —
``invoke(operator, capability, inputs, idempotency_key, *, isolation)`` — as a governed gateway
capability. The open reference backend is :class:`agentic_os.mission.membrane.LocalContainmentSandbox`
(a rootless child process with a scrubbed env, resource ceilings and a contained cwd, driven by one
``ExecutionConstraint``). The Enterprise ``runtime_security_enterprise.SubprocessSandbox`` satisfies the
SAME protocol — it *wraps* ``LocalContainmentSandbox`` with network-namespace / privilege-drop /
secret-redaction policy — so it backs this capability unchanged. There is one sandbox contract across
the AGPL floor and the enterprise overlay, per the open-core boundary (the plan's §8 "sandbox is a
separate internal capability", realised on the runtime's existing containment plane).

A capability is an importable ``"module:attr"`` compute step, ``fn(inputs) -> dict`` (the
stateless-agent contract) — not arbitrary shell. ``sandbox.execute`` is CRITICAL + mandatory-approval,
reachable only under the strictest gate; the wired backend (and the deployment) decide which targets
run and how hard the confinement is.
"""
from __future__ import annotations

from typing import Any, Optional

from .contracts import ApprovalPolicy, CapabilityKind, CapabilityManifest, DataClass, RiskTier
from .registry import CapabilityRegistry, HandlerResult


def echo_capability(inputs: dict) -> dict:
    """A trivial reference compute step (``fn(inputs) -> dict``) for demos/tests: run
    ``sandbox.execute`` with capability ``agentic_os.agent_gateway.sandbox:echo_capability`` to prove
    the containment path end-to-end (it executes in the confined child, not in-process)."""
    return {"echo": dict(inputs or {})}


def _default_sandbox():
    # Lazy: the open reference containment lives in the mission plane (pulls runtime_contracts); the
    # gateway core must import without it. A deployment injects the Enterprise SubprocessSandbox instead.
    from agentic_os.mission.membrane import LocalContainmentSandbox
    return LocalContainmentSandbox()


SANDBOX_EXECUTE = CapabilityManifest(
    "sandbox.execute",
    "Run a compute capability (module:attr) in an isolated sandbox — scrubbed env, resource-limited, "
    "no ambient credentials.",
    CapabilityKind.DIRECT, permissions=("sandbox.execute",), risk_tier=RiskTier.CRITICAL,
    side_effecting=True, approval_policy=ApprovalPolicy.MANDATORY, provider="sandbox",
    input_schema={"type": "object",
                  "properties": {"capability": {"type": "string"},
                                 "inputs": {"type": "object"},
                                 "isolation": {"type": "string", "enum": ["sandbox", "strict"]}},
                  "required": ["capability"]},
    output_schema={"type": "object"},
    data_classes=(DataClass.INTERNAL,))


def register_sandbox_capability(registry: CapabilityRegistry, sandbox: Optional[Any] = None,
                                *, default_isolation: str = "sandbox") -> CapabilityRegistry:
    """Register ``sandbox.execute`` backed by an ``agentic_os.mission.executor.Sandbox`` (the canonical
    seam). ``sandbox`` defaults to the open :class:`LocalContainmentSandbox`; inject the Enterprise
    ``SubprocessSandbox`` (same protocol) for network-namespace / privilege-drop confinement — no
    adapter, because it is the same contract."""
    sbx = sandbox if sandbox is not None else _default_sandbox()

    def _handler(req, env):
        args = req.arguments
        target = str(args.get("capability", ""))
        if ":" not in target:
            return HandlerResult(ok=False, error="a capability target 'module:attr' is required")
        isolation = str(args.get("isolation") or default_isolation) or default_isolation
        try:
            result = sbx.invoke("agent-gateway", target, dict(args.get("inputs") or {}),
                                req.intent_hash(), isolation=isolation)
        except Exception as exc:              # ContainmentError / import failure / policy violation
            return HandlerResult(ok=False, error=str(exc))
        out = result if isinstance(result, dict) else {"result": result}
        return HandlerResult(ok=True, output=out, data_classes=(DataClass.INTERNAL,))

    return registry.register(SANDBOX_EXECUTE, _handler)
