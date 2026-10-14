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

import hashlib
import json
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
COMPENSATION_EVENTS = (EV_REQUESTED, EV_AUTHORIZED, EV_EXECUTED, EV_VERIFIED, EV_FAILED)


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


def lint_capability(cap: Any) -> str | None:
    """Return a violation message for a side-effecting capability that declares neither compensation nor
    irreversibility, or that declares an undo it does not itself coherently describe; else None."""
    if not getattr(cap, "side_effecting", False):
        return None
    spec = coerce(getattr(cap, "compensation", None))
    if spec is not None and spec.supported and getattr(cap, "irreversible", False):
        return f"{cap.name}: declares both compensation.supported and irreversible"
    if spec is not None and spec.supported and cap.undo and cap.undo != spec.capability:
        return f"{cap.name}: undo={cap.undo!r} disagrees with compensation.capability={spec.capability!r}"
    if reversibility_of(cap) is Reversibility.UNKNOWN:
        return f"{cap.name}: side-effecting but declares neither `compensation` nor `irreversible=True`"
    return None


def validate_plan_compensation(nodes: Iterable[Any], get_capability) -> list[str]:
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
    return errors


def dangling_undos(caps: Iterable[Any], get_capability) -> list[str]:
    """Capabilities whose DECLARED compensating capability is itself not registered (it could never run).
    Not a plan-time refusal (legacy fleets carry such declarations; compensation then fails visibly as
    COMPENSATION_FAILED) but the conformance lint rejects it for the kernel's own apps."""
    return [f"{c.name}: compensating capability '{declared_undo(c)}' is not registered"
            for c in caps if declared_undo(c) and get_capability(declared_undo(c)) is None]


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
    or dicts). status: requested | authorized | executed_unverified | compensated | failed. A later
    ``requested`` (retry) restarts the node; unknown event types are ignored (forward compatible)."""
    out: dict[str, dict] = {}
    for e in events:
        t = e["type"] if isinstance(e, dict) else e.type
        if t not in COMPENSATION_EVENTS:
            continue
        p = (e["payload"] if isinstance(e, dict) else e.payload) or {}
        nid = p.get("node_id")
        if not nid:
            continue
        cur = out.setdefault(nid, {"status": "requested", "undo": p.get("undo"), "reason": ""})
        if t == EV_REQUESTED:
            cur.update(status="requested", undo=p.get("undo"), reason="")
        elif t == EV_AUTHORIZED:
            cur["status"] = "authorized"
        elif t == EV_EXECUTED:
            cur.update(status="executed_unverified", receipt=p.get("receipt"))
        elif t == EV_VERIFIED:
            cur.update(status="compensated", method=p.get("method"))
        elif t == EV_FAILED:
            cur.update(status="failed", reason=p.get("reason", ""))
    return out


def summarize(folded: dict[str, dict]) -> dict:
    """Mission-outcome view of a fold. ``compensated`` holds ONLY verified compensations."""
    by = lambda st: sorted(n for n, v in folded.items() if v["status"] == st)  # noqa: E731
    return {"compensated": by("compensated"), "compensated_unverified": by("executed_unverified"),
            "compensation_failed": {n: v["reason"] for n, v in sorted(folded.items()) if v["status"] == "failed"}}
