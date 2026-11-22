"""Glue between MissionRuntime and the RevisionLedger. Every function takes the runtime; none of this is app-specific.

A capability opts in with two optional, declarative dicts (see ``operator_sdk.capability``):

  revision_bound    = {"artifact": "social/post/{id}", "digest": "head" | "inputs" | "<input key>" | ["k1", "k2"], "target": "{id}", "ttl_s": 0}
      The action is bound to ONE immutable revision of the artifact. ``digest: "inputs"`` -> the artifact is the action's own inputs and a
      new revision is derived from them (no producer needed). Otherwise the digest is taken from the named input(s) and the revision must
      already exist (registered by a producer), and be the head. ``digest: "head"`` -> approve whatever the head revision is at park time (the
      human is shown it); the ledger then guarantees it is still the head when the action runs.
  revision_produces = {"artifact": "social/post/{id}", "digest": "result" | "<result key>" | ["k1", "k2"], "target": "{id}"}
      Success of this capability registers a new revision of the artifact (superseding approvals of older ones).

Templates are ``str.format`` over the resolved inputs (and, for ``revision_produces``, the result dict). A revision-bound capability is always
human-gated, and it never executes without a GRANTED, current, matching ledger approval, which is consumed (CAS) before dispatch.
"""
from __future__ import annotations

from typing import Any

from .revisions import ApprovalRefused, RevisionError, RevisionLedger, content_digest

REVISION_KEY = "revision"           # where the binding is carried inside NodeParked / ApprovalGranted payloads


class GateError(RevisionError):
    """The binding could not be established or checked; the node must not park/run."""


def _tenant(m) -> str:
    return getattr(m, "tenant", "") or "default"


def _actor(m) -> str:
    return f"mission:{m.id}"


class _Strict(dict):
    """format_map source that refuses a missing OR empty field: an artifact key like ``social/post/None`` must never be bound or registered."""

    def __getitem__(self, k):
        v = dict.__getitem__(self, k)
        if v is None or v == "" or isinstance(v, (dict, list)):
            raise KeyError(k)
        return v


def _fmt(template: Any, source: dict, what: str) -> str:
    if not isinstance(template, str) or not template:
        return ""
    try:
        return template.format_map(_Strict(source))
    except (KeyError, IndexError, ValueError, AttributeError) as e:
        raise GateError("binding_unresolvable", f"{what} template {template!r} cannot be filled from the available fields: {e!r}") from e


def _digest(spec: Any, source: dict, what: str) -> str:
    if isinstance(spec, str) and spec not in ("result",):
        keys = [spec]
    elif isinstance(spec, (list, tuple)) and spec:
        keys = list(spec)
    else:
        raise GateError("binding_unresolvable", f"{what} digest spec {spec!r} is not a field name, a list of field names, 'inputs' or 'result'")
    missing = [k for k in keys if k not in source]
    if missing:
        raise GateError("binding_unresolvable", f"{what} digest field(s) missing: {missing}")
    return content_digest({k: source[k] for k in keys} if len(keys) > 1 or isinstance(spec, (list, tuple)) else source[keys[0]])


def _inputs(rt, m, world, node) -> dict:
    d = dict(rt._resolve_inputs(m, world, node))
    d.pop("_approval", None)
    d.pop("_node", None)
    return d


def binding(rt, m, world, node) -> tuple[str, str, str, bool]:
    """(artifact_key, digest, target, implicit) the node is about to act on, from its CURRENT resolved inputs."""
    b = node.revision_bound or {}
    inp = _inputs(rt, m, world, node)
    key = _fmt(b.get("artifact"), inp, "revision_bound.artifact")
    if not key:
        raise GateError("binding_unresolvable", "revision_bound needs an 'artifact' template")
    target = _fmt(b.get("target"), inp, "revision_bound.target") if b.get("target") else ""
    spec = b.get("digest", "inputs")
    if spec == "inputs":
        return key, rt._node_inputs_digest(m, world, node), target, True
    if spec == "head":                      # approve whatever the artifact's head revision is NOW; consume still requires it to be unchanged
        head = _ledger(rt).head(key)
        if head is None:
            raise GateError("unknown_artifact", f"artifact {key!r} has no registered revision to approve")
        return key, head.digest, target, False
    return key, _digest(spec, inp, "revision_bound"), target, False


def _ledger(rt) -> RevisionLedger:
    led = getattr(rt, "revisions", None)
    if led is None:
        raise GateError("no_ledger", "capability is revision_bound but this runtime has no RevisionLedger; refusing (fail closed)")
    return led


# ── park: open an approval bound to the head revision ────────────────────────────────────────────────────────────
def on_park(rt, m, world, node) -> dict:
    led = _ledger(rt)
    key, digest, target, implicit = binding(rt, m, world, node)
    ttl = float((node.revision_bound or {}).get("ttl_s") or 0.0)
    try:
        if implicit:
            rev = led.register_revision(key, digest, actor=_actor(m), tenant=_tenant(m), meta={"target": target, "capability": node.capability, "mission_id": m.id, "implicit": True})
        else:
            rev = led.head(key)
            if rev is None:
                raise GateError("unknown_artifact", f"artifact {key!r} has no registered revision")
            if rev.digest != digest:
                raise GateError("stale_revision", f"this action refers to content that is not the current revision of {key!r} (head is revision {rev.rev})", head_rev=rev.rev)
        ap = led.request_approval(artifact_key=key, rev=rev.rev, digest=digest, action=node.capability, target=target, tenant=_tenant(m), principal=_actor(m),
                                  mission_id=m.id, node_id=node.id, ttl_s=ttl)
    except ApprovalRefused as e:
        raise GateError(e.code, e.detail, **e.info) from e
    if implicit and rev.superseded:
        # This park created the revision, so it superseded other gates. This mission's OWN earlier gate (inputs changed -> ApprovalInvalidated -> re-park)
        # is simply being re-raised: record it as handled. Every other mission waiting on the old revision is told now.
        for aid in rev.superseded:
            old = led.approval(aid)
            if old is not None and old.mission_id == m.id:
                led.mark_reconciled(aid)
        reconcile(rt, held=m.id)
    return {"artifact_key": key, "rev": ap.rev, "digest": digest, "target": target, "approval_id": ap.approval_id, "action": node.capability}


def parked_binding(rt, mission_id: str, node_id: str) -> dict | None:
    """The revision binding recorded when this node was last parked (None when the node is not revision-bound)."""
    out = None
    for e in rt.store.for_mission(mission_id):
        if e.type == "NodeParked" and e.payload.get("node_id") == node_id:
            out = e.payload.get(REVISION_KEY) or None
    return out


def granted_binding(rt, mission_id: str, node_id: str) -> dict | None:
    out = None
    for e in rt.store.for_mission(mission_id):
        if e.type == "ApprovalGranted" and e.payload.get("node_id") == node_id:
            out = e.payload.get(REVISION_KEY) or None
        elif e.type == "ApprovalInvalidated" and e.payload.get("node_id") == node_id:
            out = None
    return out


# ── human decision ───────────────────────────────────────────────────────────────────────────────────────────────
def on_decision(rt, m, node_id: str, *, approve: bool, actor: str) -> dict | None:
    """Record the human's decision in the ledger. Returns the binding to stamp on ApprovalGranted, or None when the node is not revision-bound.
    Raises ApprovalRefused when the approval was superseded/stale/expired (the mission log is reconciled first, so nothing stays parked)."""
    b = parked_binding(rt, m.id, node_id)
    if not b:
        return None
    led = _ledger(rt)
    who = actor or "unattributed"
    if not approve:
        try:
            led.reject(b["approval_id"], approver=who, reason="rejected by approver")
        except ApprovalRefused:
            pass                                    # already superseded/decided: the rejection of the gate still stands
        return b
    try:
        led.grant(b["approval_id"], approver=who)
    except ApprovalRefused as e:
        reconcile(rt, held=m.id)
        raise
    return b


# ── dispatch: the atomic revalidate-and-consume ──────────────────────────────────────────────────────────────────
def admit(rt, m, plan, node) -> bool:
    """Called right after ``_pre_exec_ok`` on every dispatch path. True = the node may be dispatched now (the ledger approval has been CONSUMED)."""
    if not getattr(node, "revision_bound", None):
        return True
    world = rt._world(m.id)
    try:
        led = _ledger(rt)
        b = granted_binding(rt, m.id, node.id)
        if not b:
            raise GateError("no_revision_approval", f"{node.capability} is revision-bound and has no revision-bound human approval on this mission")
        if (node.revision_bound or {}).get("digest") == "head":
            # the ledger decides whether the approved revision is still the head (code ``superseded``/``stale_revision``), not a digest comparison here
            inp = _inputs(rt, m, world, node)
            key = _fmt(node.revision_bound.get("artifact"), inp, "revision_bound.artifact")
            target = _fmt(node.revision_bound.get("target"), inp, "revision_bound.target") if node.revision_bound.get("target") else ""
            digest = b["digest"]
        else:
            key, digest, target, _ = binding(rt, m, world, node)
        if (key, target) != (b["artifact_key"], b["target"]) or digest != b["digest"]:
            raise GateError("binding_mismatch", "what is about to run is not what was approved (artifact, target or content changed)")
        led.consume(b["approval_id"], artifact_key=key, rev=b["rev"], digest=digest, action=node.capability, target=target, tenant=_tenant(m), executor=_actor(m))
    except (ApprovalRefused, GateError) as e:
        code = getattr(e, "code", "refused")
        rt.store.append("RevisionApprovalRefused", m.id, {"node_id": node.id, "capability": node.capability, "code": code, "detail": getattr(e, "detail", str(e)),
                                                         **{k: v for k, v in getattr(e, "info", {}).items() if isinstance(v, (str, int, float, bool, type(None)))}})
        rt.store.append("ApprovalInvalidated", m.id, {"node_id": node.id, "capability": node.capability, "reason": f"revision approval refused: {code}"})
        rt._fail(m, plan, reason=f"revision_approval_refused: {code}")
        return False
    rt.store.append("ApprovalConsumed", m.id, {"node_id": node.id, "capability": node.capability, REVISION_KEY: b})
    return True


# ── production: a successful producer registers a new revision ───────────────────────────────────────────────────
def after_success(rt, m, node, result: Any) -> None:
    spec = getattr(node, "revision_produces", None)
    led = getattr(rt, "revisions", None)
    if not spec or led is None or not isinstance(result, dict):
        return
    try:
        world = rt._world(m.id)
        src = {**_inputs(rt, m, world, node), **result}
        key = _fmt(spec.get("artifact"), src, "revision_produces.artifact")
        target = _fmt(spec.get("target"), src, "revision_produces.target") if spec.get("target") else ""
        digest = content_digest(result) if spec.get("digest", "result") == "result" else _digest(spec.get("digest"), src, "revision_produces")
        rev = led.register_revision(key, digest, actor=_actor(m), tenant=_tenant(m), meta={"target": target, "capability": node.capability, "mission_id": m.id, "node_id": node.id})
        rt.store.append("RevisionRegistered", m.id, {"node_id": node.id, "artifact_key": key, "rev": rev.rev, "digest": digest, "created": rev.created, "superseded": list(rev.superseded)})
    except Exception as e:  # noqa: BLE001 — the node succeeded; a ledger fault must not rewrite that. Consume still checks the head, so this is safe.
        rt.store.append("RevisionRegistrationFailed", m.id, {"node_id": node.id, "error": f"{type(e).__name__}: {e}"[:300]})
        return
    reconcile(rt, held=m.id)


# ── reconciliation: tell missions their gate was superseded ──────────────────────────────────────────────────────
def reconcile(rt, held: str | None = None) -> list[str]:
    """Drain superseded approvals into the mission logs of this runtime: ApprovalSuperseded + ApprovalInvalidated + NodeSkipped, and the mission is
    cancelled (it was about to act on content that no longer exists; the app starts a new mission for the new revision). ``held`` is the mission
    whose run lock the caller already holds. Returns the ids reconciled."""
    led = getattr(rt, "revisions", None)
    if led is None:
        return []
    done = []
    for a in led.unreconciled_supersessions():
        m = rt._missions.get(a.mission_id)
        if m is None:
            continue                                      # another runtime/process owns this mission; it reconciles it
        lk = rt._run_lock(a.mission_id)
        with lk:                                          # RLock: re-entrant when the caller already holds this mission's lock
            from .types import MissionState  # noqa: PLC0415
            if m.state not in (MissionState.SUCCEEDED, MissionState.FAILED, MissionState.CANCELLED, MissionState.COMPENSATION_FAILED):
                rt.store.append("ApprovalSuperseded", m.id, {"node_id": a.node_id, "approval_id": a.approval_id, "artifact_key": a.artifact_key, "rev": a.rev,
                                                            "superseded_by_rev": a.superseded_by_rev, "reason": a.superseded_reason})
                rt.store.append("ApprovalInvalidated", m.id, {"node_id": a.node_id, "reason": f"superseded: {a.superseded_reason}"})
                rt.store.append("NodeSkipped", m.id, {"node_id": a.node_id, "reason": "approval superseded by a newer revision"})
                rt.cancel_mission(m.id, reason=f"approval superseded: {a.superseded_reason}")
        led.mark_reconciled(a.approval_id)
        done.append(a.approval_id)
    return done
