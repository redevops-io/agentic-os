"""Saga-style compensation contract (docs/COMPENSATION.md) — vocabulary, capability metadata, plan validation
and the log fold. NOT automatic rollback: ``COMPENSATION = EXPLICIT_ONLY``.

A consequential (side-effecting) capability DECLARES whether it supports compensation and names the
compensating capability. Nothing is ever assumed reversible:

  REVERSIBLE_EXPLICIT   a compensating capability is declared (``compensation.capability`` or legacy ``undo``)
  IRREVERSIBLE          declared irreversible (``irreversible=True`` / ``compensation.reversibility``)
  UNKNOWN               side-effecting with no declaration at all -> treated as irreversible
  NOT_APPLICABLE        not side-effecting

Pure module: no runtime/executor imports, so ``compensation_policy`` and the fork/join code can use it freely.
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Any, Iterable

COMPENSATION_MODE = "EXPLICIT_ONLY"

# events (appended to the durable mission log; every payload carries ``node_id``)
EV_REQUESTED = "CompensationRequested"
EV_AUTHORIZED = "CompensationAuthorized"
EV_EXECUTED = "CompensationExecuted"
EV_VERIFIED = "CompensationVerified"
EV_FAILED = "CompensationFailed"
EV_APPROVAL_REQUIRED = "CompensationApprovalRequired"   # parked: the compensation needs its OWN human approval
EV_APPROVED = "CompensationApproved"                     # who/when/what (undo, inputs hash, attempt) was approved
EV_ACKNOWLEDGED = "CompensationAcknowledged"             # a human accepts that the forward effect remains
EV_UNDECLARED_TARGET = "CompensationUndeclaredTarget"    # warning: declared undo is not a registered capability
COMPENSATION_EVENTS = (EV_REQUESTED, EV_AUTHORIZED, EV_EXECUTED, EV_VERIFIED, EV_FAILED, EV_APPROVAL_REQUIRED,
                       EV_APPROVED, EV_ACKNOWLEDGED, EV_UNDECLARED_TARGET)

ENV_MAX_ATTEMPTS = "AGENTIC_OS_COMPENSATION_MAX_ATTEMPTS"
ENV_STRICT_TARGETS = "AGENTIC_OS_STRICT_UNDO_TARGETS"
#: After this date a plan whose node declares an undo capability that is not registered is REFUSED at plan time
#: by default (before it: allowed with a visible CompensationUndeclaredTarget warning when compensation is
#: requested, and the unregistered target never receives the compensation marker). Override with the env flag.
STRICT_UNDO_SUNSET = _dt.date(2027, 1, 1)


def max_attempts() -> int:
    try:
        return max(1, int(os.environ.get(ENV_MAX_ATTEMPTS, "3")))
    except ValueError:
        return 3


def strict_undo_targets(today: "_dt.date | None" = None) -> bool:
    raw = os.environ.get(ENV_STRICT_TARGETS, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return (today or _dt.date.today()) >= STRICT_UNDO_SUNSET


class Reversibility(str, Enum):
    REVERSIBLE_EXPLICIT = "REVERSIBLE_EXPLICIT"
    IRREVERSIBLE = "IRREVERSIBLE"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass
class CompensationSpec:
    """``compensation: {supported, capability, reversibility, requires_approval, verify, notes}``."""
    supported: bool = False
    capability: str | None = None           # the compensating capability id (required when supported)
    reversibility: str = ""                 # "" = derive; else a Reversibility value
    requires_approval: bool = False         # compensation needs a human approval of its own (see docs)
    verify: str | None = None               # read-back capability that proves the effect is gone
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def coerce(value: Any) -> CompensationSpec | None:
    """dict | CompensationSpec | None -> CompensationSpec | None (unknown keys are rejected: fail closed)."""
    if value is None or isinstance(value, CompensationSpec):
        return value
    if isinstance(value, dict):
        allowed = set(CompensationSpec.__dataclass_fields__)
        extra = set(value) - allowed
        if extra:
            raise ValueError(f"unknown compensation field(s): {sorted(extra)}")
        spec = CompensationSpec(**value)
        if spec.reversibility and spec.reversibility not in Reversibility.__members__:
            raise ValueError(f"unknown reversibility {spec.reversibility!r}")
        if spec.supported and not spec.capability:
            raise ValueError("compensation.supported requires compensation.capability")
        if not spec.supported and spec.reversibility == "REVERSIBLE_EXPLICIT":
            raise ValueError("compensation.supported=False contradicts reversibility=REVERSIBLE_EXPLICIT")
        if not spec.supported and spec.capability:
            raise ValueError("compensation.supported=False must not name a compensating capability")
        if spec.supported and spec.reversibility not in ("", "REVERSIBLE_EXPLICIT"):
            raise ValueError("compensation.supported=True requires reversibility REVERSIBLE_EXPLICIT")
        return spec
    raise TypeError(f"compensation must be a dict or CompensationSpec, got {type(value).__name__}")


def declared_undo(cap: Any) -> str | None:
    """The compensating capability a capability DECLARES (explicit spec first, then the legacy ``undo``)."""
    spec = coerce(getattr(cap, "compensation", None))
    if spec is not None and spec.supported and spec.capability:
        return spec.capability
    return getattr(cap, "undo", None) or None


def reversibility_of(cap: Any) -> Reversibility:
    if cap is None or not getattr(cap, "side_effecting", False):
        return Reversibility.NOT_APPLICABLE
    if declared_undo(cap):
        return Reversibility.REVERSIBLE_EXPLICIT
    spec = coerce(getattr(cap, "compensation", None))
    # an explicit `compensation: {supported: False}` is itself a declaration: "not compensable"
    if getattr(cap, "irreversible", False) or (spec is not None and not spec.supported
                                               and spec.reversibility != "UNKNOWN"):
        return Reversibility.IRREVERSIBLE
    return Reversibility.UNKNOWN


def is_declared(cap: Any) -> bool:
    """True when a consequential capability made an explicit decision (what the conformance lint demands)."""
    return reversibility_of(cap) in (Reversibility.REVERSIBLE_EXPLICIT, Reversibility.IRREVERSIBLE,
                                     Reversibility.NOT_APPLICABLE)


def irreversible_note(cap: Any) -> str:
    spec = coerce(getattr(cap, "compensation", None))
    return ((getattr(cap, "irreversible_note", "") or "") or (spec.notes if spec is not None else "")).strip()


def intrinsic_problems(cap: Any) -> list[str]:
    """Registration-time checks that need no other capability (CapabilityRegistry.register raises on these)."""
    out: list[str] = []
    name = getattr(cap, "name", "?")
    spec = coerce(getattr(cap, "compensation", None))
    undo = declared_undo(cap)
    if undo and undo == name:
        out.append(f"{name}: compensation target is the capability itself (would re-run the forward action)")
    if spec is not None and spec.verify and spec.verify in (name, undo):
        out.append(f"{name}: verify '{spec.verify}' must be a read-back distinct from the forward action and "
                   "the compensating capability (verify == undo would run the undo twice)")
    if spec is not None and spec.supported and getattr(cap, "irreversible", False):
        out.append(f"{name}: declares both compensation.supported and irreversible")
    if getattr(cap, "undo", None) and spec is not None and spec.supported and cap.undo != spec.capability:
        out.append(f"{name}: undo={cap.undo!r} disagrees with compensation.capability={spec.capability!r}")
    return out


def lint_capability(cap: Any) -> str | None:
    """A violation message for a side-effecting capability that declares neither compensation nor
    irreversibility (with a note), or declares them incoherently; else None. Pure per-capability checks."""
    if not getattr(cap, "side_effecting", False):
        return None
    probs = intrinsic_problems(cap)
    if probs:
        return "; ".join(probs)
    cls = reversibility_of(cap)
    if cls is Reversibility.UNKNOWN:
        return f"{cap.name}: side-effecting but declares neither `compensation` nor `irreversible=<note>`"
    if cls is Reversibility.IRREVERSIBLE and not irreversible_note(cap):
        return f"{cap.name}: irreversible requires a non-empty note (why it cannot be undone)"
    return None


def registry_problems(caps: Iterable[Any], get_capability, *, require_verify: bool = True) -> list[str]:
    """Cross-capability checks for a set of capabilities (the lint; ``get_capability`` resolves a name):
    nonexistent compensating target, a target that is itself a forward action pointing back (cycle), a
    ``verify`` that is unregistered or side-effecting, and (``require_verify``) a reversible declaration with
    no ``verify`` read-back."""
    out: list[str] = []
    for c in caps:
        if not getattr(c, "side_effecting", False):
            continue
        undo = declared_undo(c)
        spec = coerce(getattr(c, "compensation", None))
        if undo:
            t = get_capability(undo)
            if t is None:
                out.append(f"{c.name}: compensating capability '{undo}' is not registered")
            elif declared_undo(t) == c.name:
                out.append(f"{c.name}: compensation cycle with '{undo}' (each names the other as its undo)")
        if spec is not None and spec.supported:
            if not spec.verify:
                if require_verify:
                    out.append(f"{c.name}: reversible compensation declares no `verify` read-back")
            else:
                v = get_capability(spec.verify)
                if v is None:
                    out.append(f"{c.name}: verify '{spec.verify}' is not registered")
                elif getattr(v, "side_effecting", False):
                    out.append(f"{c.name}: verify '{spec.verify}' is side-effecting (must be a read-only read-back)")
    return out


def validate_plan_compensation(nodes: Iterable[Any], get_capability, *, strict: bool = False) -> list[str]:
    """Plan-time refusal list: a node that carries an ``undo`` the capability registry does not declare for
    its capability (planner-attached: "undeclared"). ``get_capability``
    is ``registry.get`` (the UNSCOPED trusted registry). Empty list = plan may run."""
    errors: list[str] = []
    for n in nodes:
        undo = getattr(n, "undo", None)
        if not undo:
            continue
        cap = get_capability(n.capability)
        registered = declared_undo(cap) if cap is not None else None
        if registered != undo:
            errors.append(f"node {getattr(n, 'id', '?')} ({n.capability}): undo '{undo}' is not the registered "
                          f"undo ({registered!r}) of this capability")
        elif strict and get_capability(undo) is None:
            errors.append(f"node {getattr(n, 'id', '?')} ({n.capability}): undo capability '{undo}' is not registered "
                          f"(refused after {STRICT_UNDO_SUNSET.isoformat()} / {ENV_STRICT_TARGETS}=1)")
        elif undo == n.capability:
            errors.append(f"node {getattr(n, 'id', '?')} ({n.capability}): undo is the forward capability itself")
    return errors


def dangling_undos(caps: Iterable[Any], get_capability) -> list[str]:
    """Capabilities whose DECLARED compensating capability is itself not registered (it could never run).
    Not a plan-time refusal (legacy fleets carry such declarations; compensation then fails visibly as
    COMPENSATION_FAILED) but the conformance lint rejects it for the kernel's own apps."""
    return [f"{c.name}: compensating capability '{declared_undo(c)}' is not registered"
            for c in caps if declared_undo(c) and get_capability(declared_undo(c)) is None]


def undo_inputs(node: Any, mission_id: str, forward_result: Any) -> dict:
    """The EXACT inputs a compensating dispatch carries: derived from the logged forward result, never from
    plan fields. ``_node`` is the compensation's own node id, so a signed token is bound to the compensation
    and cannot be the forward step's token."""
    return {"undo_of": node.capability, "result": copy.deepcopy(forward_result),
            "_node": f"{node.id}:undo", "_mission": mission_id}


def receipt_for(node_id: str, undo: str, idempotency_key: str, result: Any) -> dict:
    """The compensation's OWN receipt: what was invoked, under which key, and a digest of what came back."""
    try:
        blob = json.dumps(result, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        blob = repr(result)
    return {"node_id": node_id, "undo": undo, "idempotency_key": idempotency_key,
            "result_sha256": hashlib.sha256(blob.encode()).hexdigest()}


# ── fold (replay) ────────────────────────────────────────────────────────────────────────────────────────────
def fold_compensation(events: Iterable[Any]) -> dict[str, dict]:
    """Per-node compensation status folded from the durable log (events are objects with ``type``/``payload``
    or dicts). status: requested | approval_required | authorized | executed_unverified | compensated | failed
    | acknowledged. ``attempts`` counts CompensationRequested; a later ``requested`` (retry) restarts the node.
    ``approval`` is the latest CompensationApproved for the CURRENT attempt. Unknown event types are ignored."""
    out: dict[str, dict] = {}
    for e in events:
        t = e["type"] if isinstance(e, dict) else e.type
        if t not in COMPENSATION_EVENTS:
            continue
        p = (e["payload"] if isinstance(e, dict) else e.payload) or {}
        nid = p.get("node_id")
        if not nid:
            continue
        cur = out.setdefault(nid, {"status": "requested", "undo": p.get("undo"), "reason": "", "attempts": 0,
                                   "attempt": 0, "approval": None, "method": None})
        if t == EV_REQUESTED:
            cur.update(status="requested", undo=p.get("undo"), reason="", approval=None, method=None,
                       attempt=int(p.get("attempt") or cur["attempts"] + 1))
            cur["attempts"] += 1
        elif t == EV_APPROVAL_REQUIRED:
            cur.update(status="approval_required", reason="awaiting_compensation_approval",
                       inputs_sha256=p.get("inputs_sha256"))
        elif t == EV_APPROVED:
            if int(p.get("attempt") or 0) == cur["attempt"]:
                cur["approval"] = dict(p)
        elif t == EV_AUTHORIZED:
            cur["status"] = "authorized"
        elif t == EV_EXECUTED:
            cur.update(status="executed_unverified", receipt=p.get("receipt"))
        elif t == EV_VERIFIED:
            cur.update(status="compensated", method=p.get("method"), actor=p.get("actor"))
        elif t == EV_FAILED:
            cur.update(status="failed", reason=p.get("reason", ""))
        elif t == EV_ACKNOWLEDGED:
            cur.update(status="acknowledged", actor=p.get("actor"), ack_reason=p.get("reason", ""))
    return out


def summarize(folded: dict[str, dict]) -> dict:
    """Mission-outcome view of a fold. ``compensated`` holds ONLY verified compensations."""
    by = lambda st: sorted(n for n, v in folded.items() if v["status"] == st)  # noqa: E731
    return {"compensated": by("compensated"), "compensated_unverified": by("executed_unverified"),
            "compensation_failed": {n: v["reason"] for n, v in sorted(folded.items())
                                    if v["status"] in ("failed", "approval_required")},
            "compensation_acknowledged": by("acknowledged"),
            "readback_only": all(v.get("method") == "readback" for v in folded.values()
                                 if v["status"] == "compensated")}
