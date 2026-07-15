"""P7B — the Verification Plane: acceptance control for consequential state transitions.

The invariant: *no consequential operator result becomes authoritative World State until the
applicable acceptance policy succeeds.* A result runs the pipeline

    Operator Result → syntactic → policy → state-transition (assertions)
                    → ACCEPT   —or—   REJECT (hard) / ESCALATE (human decides)

Verification is a runtime plane, not merely a node type: the compiler may attach assertions in
advance, and the runtime invokes the verifier before accepting any consequential transition.
"""
from __future__ import annotations

from typing import Callable, Protocol

from .registry import CapabilityRegistry
from .types import StateAssertion, VerificationDecision, VerificationResult


class Verifier(Protocol):
    def verify(self, node, result, world, mission) -> VerificationResult: ...


def needs_verification(node) -> bool:
    """Consequential = a side effect, or a node carrying explicit assertions to check."""
    return bool(getattr(node, "side_effecting", False) or getattr(node, "assertions", None))


class CompositeVerifier:
    """Default acceptance pipeline. Hard failures (malformed result / lost permission) REJECT;
    unmet assertions ESCALATE to a human; otherwise ACCEPT."""

    def __init__(self, registry: CapabilityRegistry, granted_of: Callable[[object], set]):
        self.registry = registry
        self.granted_of = granted_of

    def verify(self, node, result, world, mission) -> VerificationResult:
        checks: list[dict] = []
        cap = self.registry.get(node.capability)

        # 1. syntactic — the result carries the capability's declared outputs
        outs = list((cap.outputs or {}).keys()) if cap else []
        syn_ok = isinstance(result, dict) and all(k in result for k in outs)
        checks.append({"stage": "syntactic", "passed": syn_ok, "detail": f"expects outputs {outs}"})

        # 2. policy — the capability's permissions still hold under the mission's grants
        granted = self.granted_of(mission)
        pol_ok = "*" in granted or cap is None or all(p in granted for p in cap.permissions)
        checks.append({"stage": "policy", "passed": pol_ok, "detail": "permissions still granted"})

        # 3. state-transition — every attached assertion holds against the result / world
        unmet = []
        for a in getattr(node, "assertions", []) or []:
            holds = _assert_holds(a, result, world)
            checks.append({"stage": "assertion", "passed": holds,
                           "detail": f"{a.key} {a.op} {a.expected!r}"})
            if not holds:
                unmet.append(a.key)

        if not syn_ok or not pol_ok:
            decision = VerificationDecision.REJECT
        elif unmet:
            decision = VerificationDecision.ESCALATE
        else:
            decision = VerificationDecision.ACCEPT
        conf = sum(1 for c in checks if c["passed"]) / max(1, len(checks))
        return VerificationResult(decision=decision, confidence=round(conf, 3),
                                  checks=checks, verifier="composite")


def _assert_holds(a: StateAssertion, result, world) -> bool:
    val = result.get(a.key) if isinstance(result, dict) and a.key in result else (
        world.get(a.key) if world is not None else None)
    a.observed = val
    if a.op == "truthy":
        a.holds = bool(val)
    elif a.op == "equals":
        a.holds = val == a.expected
    elif a.op == "in":
        a.holds = val in (a.expected or [])
    else:  # present
        a.holds = val is not None
    return bool(a.holds)
