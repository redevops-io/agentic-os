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
EV_SKIPPED = "CompensationSkipped"                       # declared precondition not met / not evaluable: NOT executed
EV_RETAINED = "EffectsRetained"                          # mission-level record of every effect that remains
COMPENSATION_EVENTS = (EV_REQUESTED, EV_AUTHORIZED, EV_EXECUTED, EV_VERIFIED, EV_FAILED, EV_APPROVAL_REQUIRED,
                       EV_APPROVED, EV_ACKNOWLEDGED, EV_UNDECLARED_TARGET, EV_SKIPPED)

#: Per-action disposition on failure (``CompensationSpec.disposition``). "" == COMPENSATE: a completed action that
#: declares a compensation is eligible when the mission's failure policy is COMPENSATE_DECLARED. PRESERVE: this
#: action's effect is KEPT on failure whatever the policy (completed beneficial / security / compliance actions);
#: it is recorded as a retained effect and never reversed automatically.
#: CLEANUP: a TEMPORARY resource this mission created (lock, staging file/dir, scratch resource) that is cleaned up on
#: failure even under ``PRESERVE_ONLY``, because removing it never removes a successful, beneficial effect. A CLEANUP
#: declaration must be fully guarded: ``supported`` + ``preconditions`` (independently safe NOW) + an explicit
#: ``authorization_policy`` (authorized) - otherwise it does not validate. It goes through the same runner as any
#: compensation (own approval/key/receipt/verification); only the POLICY gate differs.
DISPOSITIONS = ("", "COMPENSATE", "PRESERVE", "CLEANUP")
PRESERVE = "PRESERVE"
CLEANUP = "CLEANUP"

#: Explicit ``authorization_policy`` of a compensation. HUMAN_APPROVAL == ``requires_approval=True`` (a human
#: approves each reversal, bound to the exact inputs). EVIDENCE_BOUND == the evidence-bound marker alone (the
#: forward step demonstrably ran in this mission and the undo is the registered one): for non-consequential,
#: mission-owned artifacts only. Declaring one on purpose is what ``full_declaration_problems`` asks for.
AUTH_HUMAN = "HUMAN_APPROVAL"
AUTH_EVIDENCE = "EVIDENCE_BOUND"
AUTH_POLICIES = ("", AUTH_HUMAN, AUTH_EVIDENCE)

ENV_MAX_ATTEMPTS = "AGENTIC_OS_COMPENSATION_MAX_ATTEMPTS"
ENV_STRICT_TARGETS = "AGENTIC_OS_STRICT_UNDO_TARGETS"
ENV_PRECONDITION_TIMEOUT = "AGENTIC_OS_PRECONDITION_TIMEOUT_S"


def precondition_timeout_s() -> float:
    try:
        return max(0.05, float(os.environ.get(ENV_PRECONDITION_TIMEOUT, "30")))
    except ValueError:
        return 30.0

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
    disposition: str = ""                   # "" | COMPENSATE | PRESERVE (per-action; see DISPOSITIONS)
    preconditions: str | None = None        # read-only capability: "is it still safe to reverse NOW?" -> {holds: bool}
    authorization_policy: str = ""          # "" | HUMAN_APPROVAL (== requires_approval) | EVIDENCE_BOUND
    incurs_ongoing_cost: bool = False       # the retained effect keeps costing money (provisioned infrastructure):
                                            # a failed mission REPORTS its resource ids + estimated cost (or "unknown")

    def to_dict(self) -> dict:
        return asdict(self)


def coerce(value: Any) -> CompensationSpec | None:
    """dict | CompensationSpec | None -> CompensationSpec | None (unknown keys are rejected: fail closed)."""
    if value is None:
        return None
    if isinstance(value, CompensationSpec):
        # an INSTANCE is validated and normalised exactly like a dict (a hand-built spec must not bypass the
        # checks, e.g. authorization_policy=HUMAN_APPROVAL with requires_approval=False). Returns a normalised copy.
        value = asdict(value)
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
        if spec.disposition not in DISPOSITIONS:
            raise ValueError(f"unknown compensation disposition {spec.disposition!r}")
        if spec.disposition == PRESERVE and spec.supported:
            raise ValueError("compensation.disposition=PRESERVE contradicts supported=True: a preserved effect is "
                             "never automatically reversed, so declare supported=False (reversal is a new, "
                             "separately authorized decision)")
        if spec.preconditions and not spec.supported:
            raise ValueError("compensation.preconditions requires supported=True")
        if spec.disposition == CLEANUP:
            if not spec.supported:
                raise ValueError("compensation.disposition=CLEANUP requires supported=True and a compensating capability")
            if not spec.preconditions:
                raise ValueError("compensation.disposition=CLEANUP requires `preconditions` (a read-only check that "
                                 "the cleanup is independently safe NOW)")
            if not (spec.authorization_policy or spec.requires_approval):
                raise ValueError("compensation.disposition=CLEANUP requires an explicit authorization_policy "
                                 "(HUMAN_APPROVAL, or EVIDENCE_BOUND for a mission-owned temporary artifact)")
            if spec.incurs_ongoing_cost:
                raise ValueError("compensation.disposition=CLEANUP is for temporary resources: a cost-bearing "
                                 "(incurs_ongoing_cost) effect is retained and reported, never auto-cleaned")
        if spec.authorization_policy not in AUTH_POLICIES:
            raise ValueError(f"unknown compensation authorization_policy {spec.authorization_policy!r}")
        if spec.authorization_policy == AUTH_EVIDENCE and spec.requires_approval:
            raise ValueError("compensation.authorization_policy=EVIDENCE_BOUND contradicts requires_approval=True")
        if spec.authorization_policy == AUTH_HUMAN:
            spec.requires_approval = True
        return spec
    raise TypeError(f"compensation must be a dict or CompensationSpec, got {type(value).__name__}")


def is_preserved(cap: Any) -> bool:
    """True when the capability declares ``disposition=PRESERVE`` (its effect survives a failed workflow)."""
    spec = coerce(getattr(cap, "compensation", None))
    return spec is not None and spec.disposition == PRESERVE


def is_cleanup(cap: Any) -> bool:
    """True when the capability declares ``disposition=CLEANUP`` (a temporary resource that PRESERVE_ONLY still cleans)."""
    spec = coerce(getattr(cap, "compensation", None))
    return spec is not None and spec.disposition == CLEANUP and bool(spec.supported)


def incurs_ongoing_cost(cap: Any) -> bool:
    spec = coerce(getattr(cap, "compensation", None))
    return spec is not None and bool(spec.incurs_ongoing_cost)


def declared_undo(cap: Any) -> str | None:
    """The compensating capability a capability DECLARES (explicit spec first, then the legacy ``undo``)."""
    spec = coerce(getattr(cap, "compensation", None))
    if spec is not None and spec.supported and spec.capability:
        return spec.capability
    if spec is not None and spec.disposition == PRESERVE:
        return None            # a PRESERVE declaration wins over a stray legacy ``undo=``: nothing is auto-undone
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
    if spec is not None and spec.disposition == PRESERVE and getattr(cap, "undo", None):
        out.append(f"{name}: declares disposition=PRESERVE and a legacy undo={cap.undo!r} (contradiction)")
    if spec is not None and spec.preconditions and spec.preconditions in (name, undo, spec.verify):
        out.append(f"{name}: preconditions '{spec.preconditions}' must be a read-only check distinct from the "
                   "forward action, the compensating capability and the verify read-back")
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


def evidence_bound_conflicts(caps: Iterable[Any], get_capability) -> list[str]:
    """K2: ``authorization_policy=EVIDENCE_BOUND`` means NO human decides this reversal, so it can never name a
    compensating capability that itself requires approval (``approval_required``): that undo can only be
    ``HUMAN_APPROVAL``. Rejected at registration (``CapabilityRegistry.register``) and by the lint."""
    out: list[str] = []
    for c in caps:
        spec = coerce(getattr(c, "compensation", None))
        if spec is None or not spec.supported or spec.authorization_policy != AUTH_EVIDENCE:
            continue
        t = get_capability(spec.capability)
        if t is not None and getattr(t, "approval_required", False):
            out.append(f"{c.name}: authorization_policy=EVIDENCE_BOUND but its compensating capability "
                       f"'{spec.capability}' requires approval (declare HUMAN_APPROVAL)")
    return out


def registry_problems(caps: Iterable[Any], get_capability, *, require_verify: bool = True) -> list[str]:
    """Cross-capability checks for a set of capabilities (the lint; ``get_capability`` resolves a name):
    nonexistent compensating target, a target that is itself a forward action pointing back (cycle), a
    ``verify`` that is unregistered or side-effecting, and (``require_verify``) a reversible declaration with
    no ``verify`` read-back."""
    caps = list(caps)
    out: list[str] = evidence_bound_conflicts(caps, get_capability)
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
            if spec.preconditions:
                pc = get_capability(spec.preconditions)
                if pc is None:
                    out.append(f"{c.name}: preconditions '{spec.preconditions}' is not registered")
                elif getattr(pc, "side_effecting", False):
                    out.append(f"{c.name}: preconditions '{spec.preconditions}' is side-effecting (must be read-only)")
    return out


def full_declaration_problems(cap: Any) -> list[str]:
    """The per-action declaration an owner can demand of every COMPENSATABLE action (``reversibility_of`` is
    REVERSIBLE_EXPLICIT): ``compensation_capability`` (``compensation.capability``), ``preconditions``,
    ``authorization_policy`` (``requires_approval``: a human approves each reversal), ``verification_method``
    (``verify``). The ``idempotency_key`` is NOT author-supplied: it is always derived by the runner as
    ``{node.idempotency_key}:undo:{attempt}`` (read-back ``...:verify``), so it cannot be forgotten or reused.
    A legacy bare ``undo=`` has no spec and reports every item. Non-compensatable capabilities report nothing."""
    if reversibility_of(cap) is not Reversibility.REVERSIBLE_EXPLICIT:
        return []
    spec = coerce(getattr(cap, "compensation", None))
    name = getattr(cap, "name", "?")
    if spec is None:
        return [f"{name}: legacy undo= with no `compensation` declaration (capability, preconditions, "
                "authorization, verification all missing)"]
    out = []
    if not spec.capability:
        out.append(f"{name}: no compensation_capability")
    if not spec.preconditions:
        out.append(f"{name}: no preconditions (read-only check that reversing is still safe)")
    if not (spec.requires_approval or spec.authorization_policy):
        out.append(f"{name}: no authorization_policy (HUMAN_APPROVAL, or EVIDENCE_BOUND for a non-consequential "
                   "mission-owned artifact)")
    if not spec.verify:
        out.append(f"{name}: no verification_method (verify read-back)")
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
    | acknowledged | skipped (precondition not met: nothing was executed). ``attempts`` counts CompensationRequested; a later ``requested`` (retry) restarts the node.
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
            # a duplicated log record must not bump `attempts` (it would exhaust the cap): dedupe by the
            # attempt number, else by event id for a legacy record that carries none
            att = int(p.get("attempt") or 0)
            eid = (e.get("id") if isinstance(e, dict) else getattr(e, "id", None)) if not att else None
            dk = ("a", att) if att else ("e", eid)
            if dk[1] and dk in cur.setdefault("_seen", set()):
                continue
            if dk[1]:
                cur["_seen"].add(dk)
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
        elif t == EV_SKIPPED:
            cur.update(status="skipped", reason=p.get("reason", ""))
    for v in out.values():
        v.pop("_seen", None)
    return out


def summarize(folded: dict[str, dict]) -> dict:
    """Mission-outcome view of a fold. ``compensated`` holds ONLY verified compensations."""
    by = lambda st: sorted(n for n, v in folded.items() if v["status"] == st)  # noqa: E731
    return {"compensated": by("compensated"), "compensated_unverified": by("executed_unverified"),
            "compensation_failed": {n: v["reason"] for n, v in sorted(folded.items())
                                    if v["status"] in ("failed", "approval_required")},
            "compensation_acknowledged": by("acknowledged"),
            "compensation_skipped": {n: v["reason"] for n, v in sorted(folded.items()) if v["status"] == "skipped"},
            "readback_only": all(v.get("method") == "readback" for v in folded.values()
                                 if v["status"] == "compensated")}
