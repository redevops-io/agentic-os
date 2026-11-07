"""Category, outcome and a fixed-template summary for the AOS-DD events and the operator-relevant base events.

One table, one rule. A row is a (category, outcome, summary) triple over the EXISTING closed vocabularies of the contract (``models.CATEGORIES`` /
``OUTCOMES``): nothing is added to a schema. A summary is a template over STRUCTURED fields of the event only (step ids, capability names, closed
tokens such as ``rule`` / ``kind`` / ``status``, booleans, counts, a principal's id). It never contains a free-text field: an application's
``message``, a reason an operator typed, a ``why`` / ``detail`` / ``next`` / ``state`` / ``action`` sentence. Every inserted value goes through
:func:`tok` (a short token: letters, digits and ``_ . : @ / + # -`` and spaces; anything else is replaced; at most 80 characters), so a summary has no
markup and no control characters whatever the event holds, and a summary is at most 240 characters. An event type that is not in the table keeps the
old behaviour (category ``other``, outcome ``info``, the bare type name): see ``fold.py``.

The wording says what the event records and no more. In particular a late (stale) result is "recorded, not applied" and a duplicate effect is "possible",
an uncertain step's effect "may or may not exist", and nothing here says an effect was undone unless the event says an undo ran.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Tuple

_BAD = re.compile(r"[^A-Za-z0-9_.:@/+# -]")
MAX_SUMMARY = 240


def tok(v: Any, n: int = 80) -> str:
    """A short, safe token from a string or an integer; anything else is empty."""
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        return ""
    return re.sub(r"\s+", " ", _BAD.sub("?", str(v))).strip()[:n]


# `reason` fields that are CLOSED tokens chosen by the runtime (never typed by a person, never from an application). Anything else is left out of the summary.
_CLOSED_REASONS = frozenset({"unauthenticated", "not_authenticated", "tenant_mismatch", "scope_tenant_mismatch", "not_authorized", "unverified"})


_ACTION = re.compile(r"^[a-z][a-z_]{0,31}$")                 # the runtime's own action names (approve, reject, compensate, ...)


def _reason(p: dict) -> str:
    r = p.get("reason")
    return r if isinstance(r, str) and r in _CLOSED_REASONS else ""


def _who(p: dict) -> str:
    """The id of the principal or actor that acted: an ``actor`` / ``approver`` string, or the ``id`` of a recorded principal."""
    for key in ("actor", "approver", "principal"):
        v = p.get(key)
        if isinstance(v, dict):
            v = v.get("id")
        t = tok(v)
        if t:
            return t
    return ""


def _n(v: Any) -> int:
    return len(v) if isinstance(v, (list, tuple, dict)) else 0


def _pl(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _by(p: dict) -> str:
    w = _who(p)
    return f" by {w}" if w else ""


def _step(p: dict) -> str:
    s = tok(p.get("step"))
    return f"step {s}" if s else "a step"


def _cap(p: dict) -> str:
    return tok(p.get("capability"))


def _checks(p: dict) -> str:
    ck = p.get("checks")
    if not isinstance(ck, list):
        return ""
    ok = sum(1 for c in ck if isinstance(c, dict) and c.get("ok") is True)
    return f", {ok} of {len(ck)} checks passed"


def _kind(p: dict) -> str:
    k = tok(p.get("read_only_kind"))
    return f"; verifier: {k}" if k else ""


def _status(p: dict) -> str:
    return tok(p.get("status")).lower() or "unknown"


def _verified(p: dict) -> bool:
    return tok(p.get("status")).upper() == "VERIFIED"


# ───────────────────────── templates ─────────────────────────
def _op_applied(p):
    how = " (adopted: an existing record was reused)" if p.get("adopted") is True else " (created)" if p.get("created") is True else " (replayed)" if p.get("replayed") is True else ""
    nid = tok(p.get("native_id"))
    return f"Operation applied: {_step(p)}" + (f", native id {nid}" if nid else "") + how


def _op_refused(p):
    s = f"Operation refused by {tok(p.get('app')) or 'the application'}: {_step(p)}"
    if tok(p.get("kind")):
        s += f", {tok(p.get('kind'))}"
    if isinstance(p.get("status"), int) and not isinstance(p.get("status"), bool):
        s += f" (status {p['status']})"
    if tok(p.get("outcome")):
        s += f", {tok(p.get('outcome'))}"
    if p.get("residual"):
        s += "; residual effects recorded"
    return s


def _op_stale(p):
    s = f"Late result recorded, not applied: {_step(p)}"
    if isinstance(p.get("fence"), int) and not isinstance(p.get("fence"), bool):
        s += f", generation {p['fence']}"
    if tok(p.get("outcome")):
        s += f" ({tok(p.get('outcome'))})"
    if tok(p.get("native_id")):
        s += f", native id {tok(p.get('native_id'))}"
    return s + "; a duplicate effect is possible"


def _uncertain(p):
    app = tok(p.get("app"))
    s = f"Outcome unknown: {_step(p)}" + (f" ({app})" if app else "") + "; the effect may or may not exist"
    if isinstance(p.get("automatic_retry"), bool):
        s += f"; automatic retry: {'yes' if p['automatic_retry'] else 'no'}"
    return s


def _step_verified(p):
    return f"Read-back {_status(p)}: {_step(p)}{_checks(p)}{_kind(p)}"


def _undo_verified(p):
    res = _n(p.get("residual")) if isinstance(p.get("residual"), list) else 0
    return f"Undo read-back {_status(p)}: {_step(p)}{_checks(p)}{_kind(p)}" + (f"; residual effects recorded: {res}" if res else "")


def _reconciled(p):
    nid = tok(p.get("native_id"))
    if p.get("found") is True:
        return f"Reconciled{_by(p)}: {_step(p)}, the effect was found" + (f" (native id {nid})" if nid else "")
    if p.get("found") is False:
        return f"Reconciled{_by(p)}: {_step(p)}, the effect was not found"
    return f"Reconciled{_by(p)}: {_step(p)}"


def _resolved(p):
    ex = p.get("executed")
    return f"Reconciliation resolved{_by(p)}: " + ("recorded as executed" if ex is True else "recorded as not executed" if ex is False else "outcome recorded")


def _halted(p):
    n = _n(p.get("effects_remaining"))
    s = f"Mission halted: {_cap(p) or 'a step'}" + (f", rule {tok(p.get('rule'))}" if tok(p.get("rule")) else "")
    return s + (f"; {n} earlier effect remains" if n == 1 else f"; {n} earlier effects remain" if n else "")


def _rule(prefix: str):
    def f(p):
        return f"{prefix}: {_cap(p) or 'a step'}" + (f", rule {tok(p.get('rule'))}" if tok(p.get("rule")) else "")
    return f


def _cap_line(prefix: str, extra: Callable[[dict], str] = lambda p: ""):
    return lambda p: f"{prefix}: {_cap(p) or 'a step'}{extra(p)}"


def _effects_retained(p):
    r, i = _n(p.get("retained_effects")), _n(p.get("incomplete_steps"))
    return f"Effects retained: {r} retained, " + (f"{i} step incomplete" if i == 1 else f"{i} steps incomplete")


# type -> (category, outcome | callable(payload)->outcome, summary callable(payload)->str)
_ROWS: Dict[str, Tuple[str, Any, Callable[[dict], str]]] = {
    "AosddCaseOpened": ("lifecycle", "info", lambda p: "Case opened" + (f" (attempt {p['attempt']})" if isinstance(p.get("attempt"), int) and not isinstance(p.get("attempt"), bool) else "")),
    "AosddDuplicateSubmit": ("lifecycle", "info", lambda p: "Duplicate submission recorded" + (f" (attempt {p['attempt']})" if isinstance(p.get("attempt"), int) and not isinstance(p.get("attempt"), bool) else "")),
    "AosddVerifierProvenance": ("verification", "info", lambda p: "Verifier provenance recorded for " + _pl(_n(p.get("apps")), "application", "applications")),
    "AosddGrantIssued": ("policy", "info", lambda p: "Authority issued" + (f" ({tok(p.get('phase'))})" if tok(p.get("phase")) else "") + ": " + _pl(_n(p.get("grants")), "grant", "grants")),
    "AosddGrantRevoked": ("policy", "info", lambda p: "Authority revoked" + _by(p)),
    "AosddPlanApproved": ("approval", "ok", lambda p: "Plan approved: " + _pl(_n((p.get("claims") or {}).get("steps")) if isinstance(p.get("claims"), dict) else 0, "step", "steps")),
    "AosddApprovalRefused": ("approval", "denied", lambda p: f"Approval refused{_by(p)}" + (": the presented plan hash did not match the plan" if p.get("reason") == "plan_sha256 differs" else "")),
    "AosddOpIntent": ("node", "info", lambda p: f"Operation intended: {_step(p)}" + (f" ({', '.join(x for x in (tok(p.get('app')), tok(p.get('capability'))) if x)})" if tok(p.get("app")) or tok(p.get("capability")) else "")),
    "AosddOpApplied": ("node", "ok", _op_applied),
    "AosddOpRefused": ("node", "denied", _op_refused),
    "AosddOpReplayed": ("node", "info", lambda p: f"Recorded result reused: {_step(p)}" + (f", native id {tok(p.get('native_id'))}" if tok(p.get("native_id")) else "")),
    "AosddOpStale": ("node", "denied", _op_stale),
    "AosddOpUnresolved": ("node", "pending", lambda p: f"Operation unresolved: {_step(p)}" + (f", claim {tok(p.get('claim'))}" if tok(p.get("claim")) else "")),
    "AosddOpRetried": ("node", "info", lambda p: f"Operation retried under the same operation id: {_step(p)}"),
    "AosddOpSuperseded": ("node", "denied", lambda p: f"Claim superseded: {_step(p)}" + (f", generation {p['fence']}" if isinstance(p.get("fence"), int) and not isinstance(p.get("fence"), bool) else "")),
    "AosddOpUnknown": ("node", "pending", lambda p: f"Outcome unknown: {_step(p)}; the effect may or may not exist"),
    "AosddStepUncertain": ("node", "pending", _uncertain),
    "AosddStepVerified": ("verification", lambda p: "ok" if _verified(p) else "failed", _step_verified),
    "AosddAbsenceObserved": ("node", "pending", lambda p: f"Absence observed: {_step(p)}" + (", lookup complete" if p.get("complete_lookup") is True else ", lookup incomplete" if p.get("complete_lookup") is False else "")),
    "AosddRedispatchAuthorized": ("approval", "ok", lambda p: f"Redispatch authorized{_by(p)}: {_step(p)}" + ("; residual risk recorded" if p.get("residual_risk") else "")),
    "AosddRedispatchRefused": ("approval", "denied", lambda p: f"Redispatch refused: {_step(p)}"),
    "RedispatchRefused": ("node", "denied", _cap_line("Redispatch refused")),
    "AosddReconciled": ("node", "ok", _reconciled),
    "AosddReconcileRefused": ("node", "denied", lambda p: f"Reconciliation refused: {_step(p)}"),
    "AosddUncertainAccepted": ("node", "info", lambda p: f"Uncertain outcome accepted as it is{_by(p)}: {_step(p)}; not retried and not claimed to have happened"),
    "AosddResumeRequested": ("lifecycle", "info", lambda p: f"Resume requested{_by(p)}"),
    "AosddCompensationRequested": ("lifecycle", "info", lambda p: f"Compensation requested{_by(p)}"),
    "AosddUndoIntent": ("node", "info", lambda p: f"Undo intended: {_step(p)}" + (f", native id {tok(p.get('native_id'))}" if tok(p.get("native_id")) else "")),
    "AosddUndoResult": ("node", lambda p: "ok" if p.get("ok") is True else "failed", lambda p: ("Undo executed" if p.get("ok") is True else "Undo failed") + f": {_step(p)}"),
    "AosddUndoVerified": ("verification", lambda p: "ok" if _verified(p) else "failed", _undo_verified),
    "AosddMissionHalted": ("lifecycle", "denied", _halted),
    "AosddStepRefused": ("node", "denied", lambda p: _rule("Step refused")(p) + ("; no side effect occurred" if p.get("side_effects") == 0 and not isinstance(p.get("side_effects"), bool) else "")),
    # base events with operator meaning
    "StaleResultRecorded": ("node", "denied", lambda p: f"Late result from an earlier attempt recorded, not applied: {_cap(p) or 'a step'}; a duplicate effect is possible"),
    "AutomaticRetryRefused": ("node", "denied", lambda p: f"Automatic retry refused: {_cap(p) or 'a step'}" + ("; the outcome is uncertain" if p.get("uncertain") is True else "")),
    "ReconciliationRequired": ("node", "pending", lambda p: f"Reconciliation required: {_cap(p) or 'a step'}; the outcome of an earlier attempt is unknown"),
    "ReconciliationResolved": ("node", "ok", _resolved),
    "AuthorityLeaseUnknown": ("node", "pending", lambda p: "Authority lease state unknown for a step"),
    "CompensationRequested": ("node", "info", _cap_line("Compensation requested", lambda p: f" (undo {tok(p.get('undo'))})" if tok(p.get("undo")) else "")),
    "CompensationAuthorized": ("node", "ok", _cap_line("Compensation authorized", lambda p: f" ({tok(p.get('authority_basis'))})" if tok(p.get("authority_basis")) else "")),
    "CompensationExecuted": ("node", "ok", _cap_line("Compensation executed")),
    "CompensationVerified": ("verification", "ok", _cap_line("Compensation read back")),
    "CompensationFailed": ("node", "failed", _cap_line("Compensation failed")),
    "NodeCompensationFailed": ("node", "failed", lambda p: "Step compensation failed" + (f" (undo {tok(p.get('undo'))})" if tok(p.get("undo")) else "")),
    "CompensationDenied": ("node", "denied", lambda p: _rule("Compensation denied")(p) + ("; the effect remains" if p.get("effect_remains") is True else "")),
    "CompensationOperatorRequested": ("lifecycle", "info", lambda p: f"Compensation requested{_by(p)}" + (f" (scope {tok(p.get('scope'))})" if tok(p.get("scope")) else "")),
    "CompensationOperatorRefused": ("lifecycle", "denied", lambda p: "Compensation request refused" + (f": {_reason(p)}" if _reason(p) else "")),
    "CompensationRetryRequested": ("node", "info", lambda p: f"Compensation retry requested{_by(p)}: " + _pl(_n(p.get("node_ids")), "step", "steps")),
    "EffectsRetained": ("lifecycle", "info", _effects_retained),
    "MissionCancelled": ("lifecycle", "info", lambda p: "Mission cancelled"),
    "MissionSuspended": ("lifecycle", "info", lambda p: "Mission suspended"),
    "MissionResumed": ("lifecycle", "info", lambda p: "Mission resumed"),
    "ExecutionAuthorityRefused": ("policy", "denied", _rule("Execution authority refused")),
    "AuthorityDeclined": ("policy", "denied", _rule("Authority declined")),
    "ScopeAuthorized": ("policy", "info", _cap_line("Scope authorized")),
    "PrincipalRefused": ("policy", "denied", lambda p: "Operator action refused" + (f" ({p['action']})" if isinstance(p.get("action"), str) and _ACTION.match(p["action"]) else "") + (f": {_reason(p)}" if _reason(p) else "")),
    "TestPolicyActive": ("policy", "info", lambda p: "Test policy active" + (f": {tok(p.get('name'))}" if tok(p.get("name")) else "")),
    "TestPolicyAuthorized": ("policy", "info", lambda p: f"Test policy authorized: {_cap(p) or 'a step'}" + (f" ({tok(p.get('phase'))})" if tok(p.get("phase")) else "")),
    "ReflectionEmitted": ("lifecycle", "info", lambda p: "Reflection recorded" + (f" (success: {'yes' if p['success'] else 'no'})" if isinstance(p.get("success"), bool) else "")),
    "SchedulerConfigured": ("plan", "info", lambda p: "Scheduler configured" + (f": concurrency {p['effective_max_concurrency']}" if isinstance(p.get("effective_max_concurrency"), int) and not isinstance(p.get("effective_max_concurrency"), bool) else "")),
}

CATEGORY_OVERRIDES: Dict[str, str] = {t: r[0] for t, r in _ROWS.items()}


def known(t: str) -> bool:
    return t in _ROWS


def outcome(t: str, p: dict) -> str:
    o = _ROWS[t][1]
    return o(p) if callable(o) else o


def summary(t: str, p: dict) -> str:
    """The summary of a known type, bounded and clean. The caller has already checked ``known(t)``."""
    try:
        text = _ROWS[t][2](p if isinstance(p, dict) else {})
    except Exception:                                   # noqa: BLE001 - a malformed payload must not break the Timeline: the bare type name
        return t
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[<>\x00-\x1f\x7f]", "?", text)
    return text[:MAX_SUMMARY] or t
