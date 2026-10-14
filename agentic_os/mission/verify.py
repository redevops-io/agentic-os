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


# Positive indicators of failure. A denylist by design: the verifier must never *mint* success from a result
# that says (in any of the shapes real handlers use) that it did not succeed.
FAILED_STATUSES = frozenset({
    "error", "err", "failed", "failure", "forbidden", "unauthorized", "unavailable", "timeout", "conflict",
    "denied", "rejected", "blocked", "invalid", "not_found", "unverifiable", "pending_approval",
    "awaiting_approval", "awaiting_connector", "partial", "undeliverable", "not_configured"})
STAGED_STATUSES = frozenset({"pending_approval", "awaiting_approval", "awaiting_connector"})
_CODE_KEYS = frozenset({"status_code", "http_status", "http_code", "status"})


def _is_http_error(k: str, v) -> bool:
    if isinstance(v, bool) or not isinstance(v, int) or v < 400:
        return False
    k = k.lower()
    return k in _CODE_KEYS or k.endswith("_status") or k.endswith("_status_code") or k.endswith("_http_status")


def is_staged(result) -> bool:
    """True when the result says 'staged / awaiting approval' (the ``pending_approval()`` shape)."""
    return isinstance(result, dict) and str(result.get("status", "")).strip().lower() in STAGED_STATUSES


_NOT_EXECUTED_STATUSES = STAGED_STATUSES | frozenset({"staged", "held", "pending", "queued"})


def proves_execution(result, *, _depth: int = 0) -> bool:
    """STRICT check used to authorise compensation: True only if ``result`` positively looks like an executed
    effect. Stricter than ``is_staged``/``result_failed`` (which the commit path keeps unchanged): an empty
    dict, a truthy ``staged``, a staged-like status (pending_approval/awaiting_approval/awaiting_connector/staged/held/pending/queued, matched exactly),
    ``executed is False``, a failure shape, or any of those nested under ``result``/``results``/``items``
    all mean NOT executed."""
    if not isinstance(result, dict) or not result or _depth > _MAX_DEPTH:
        return False
    if result.get("staged") or result.get("executed") is False:
        return False
    if str(result.get("status", "")).strip().lower() in _NOT_EXECUTED_STATUSES:
        return False
    if is_staged(result) or result_failed(result)[0]:
        return False
    nested = result.get("result")
    if isinstance(nested, dict) and not proves_execution(nested, _depth=_depth + 1):
        return False
    for nk in _NESTED_KEYS:
        seq = result.get(nk)
        if isinstance(seq, list):
            for el in seq:
                if isinstance(el, dict) and not proves_execution(el, _depth=_depth + 1):
                    return False
    return True


_HTTP_ERROR_DETAILS = frozenset({
    "forbidden", "unauthorized", "not found", "not authenticated", "bad request", "method not allowed",
    "conflict", "unprocessable entity", "too many requests", "internal server error", "bad gateway",
    "service unavailable", "gateway timeout", "request timeout", "payment required", "invalid token",
    "could not validate credentials", "authentication failed", "permission denied", "access denied"})
_NESTED_KEYS = ("results", "items")
_MAX_DEPTH = 3


def _as_count(v) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return 0


def _http_error_value(k: str, v) -> bool:
    """`500` or the string `"500"` under a status-code-ish key."""
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    return _is_http_error(k, v)


def _classify(result: dict, depth: int) -> "tuple[bool, str]":
    st = result.get("status")
    if isinstance(st, str) and st.strip().lower() in FAILED_STATUSES:
        return True, f"status={st.strip().lower()}"
    if result.get("ok") is False:
        return True, "ok is False"
    if result.get("success") is False:
        return True, "success is False"
    if result.get("executed") is False:
        return True, "executed is False"
    if result.get("verified") is False:
        return True, "verified is False"
    for k in ("error", "errors"):
        if result.get(k):
            return True, f"{k} present"
    if _as_count(result.get("failed_count")) > 0:
        return True, f"failed_count={result.get('failed_count')}"
    if result.get("failures"):
        return True, "failures present"
    d = result.get("detail")
    if isinstance(d, str) and d.strip().lower().rstrip(".") in _HTTP_ERROR_DETAILS:
        return True, f"detail={d.strip()!r} (HTTP-style error body)"
    for k, v in result.items():
        if isinstance(k, str) and _http_error_value(k, v):
            return True, f"{k}={v}"
    if depth < _MAX_DEPTH:
        for nk in _NESTED_KEYS:
            seq = result.get(nk)
            if isinstance(seq, list):
                for i, el in enumerate(seq):
                    if isinstance(el, dict):
                        bad, why = _classify(el, depth + 1)
                        if bad:
                            return True, f"{nk}[{i}]: {why}"
    return False, ""


def result_failed(result, *, side_effecting: bool = False, declares_outputs: bool = True) -> "tuple[bool, str]":
    """The ONE shared classifier of failure-shaped results, used both when the operator mints N4 read-back
    markers / decides whether to idempotency-cache, and when the verifier decides whether a result may commit.
    Returns ``(failed, reason)``. A side-effecting capability that declares no outputs (``declares_outputs=
    False``) must report SOMETHING: an empty result is a failure (a write that proves nothing)."""
    if not isinstance(result, dict):
        return True, "result is not a dict"
    if side_effecting and not declares_outputs and not result:
        return True, "side-effecting capability returned an empty result"
    return _classify(result, 0)


def _bool_ish(type_name) -> bool:
    return str(type_name or "").strip().lower() in {"bool", "boolean"}


def _output_value_failed(value, type_name) -> bool:
    if value is None or value is False or (isinstance(value, str) and value == ""):
        return True
    return _bool_ish(type_name) and value == 0


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

        # 1b. outcome — the declared output VALUES must read back as success (a present-but-False marker is
        # a failure), and a result the shared classifier calls failed never commits regardless of its keys.
        side = bool(getattr(cap, "side_effecting", False) or getattr(node, "side_effecting", False))
        failed, why = result_failed(result, side_effecting=side, declares_outputs=bool(outs))
        if not failed and cap is not None and isinstance(result, dict):
            types_ = cap.outputs or {}
            bad = [k for k in outs if k in result and _output_value_failed(result[k], types_.get(k))]
            if bad:
                failed, why = True, f"declared output {bad} read back falsey"
        checks.append({"stage": "outcome", "passed": not failed,
                       "detail": why or "result carries no failure indicator"})
        syn_ok = syn_ok and not failed

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
