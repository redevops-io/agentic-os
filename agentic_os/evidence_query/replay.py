"""Replay controller: the audited, permissioned API for the three replay operations.

* ``reconstruct`` - rebuild a historical state (HISTORICAL RECONSTRUCTION): permission ``reconstruct``; NO side effects
  (the report counts tool dispatches before/after and the tests assert zero with a recording fake tool).
* ``resume`` - continue a reconstructed mission from where it stopped: permission ``resume``; it re-enters the ORIGINAL gates
  (a gated node needs a currently valid, bound, unexpired approval - the runtime enforces it).
* ``reexecute`` - re-plan against current evidence and run again: permission ``reexecute``; every node goes through the
  original approval/policy gates of the new revision.

The enforcement is NOT here: it lives in ``MissionRuntime`` (``grant_resume`` / ``_require_execution_authority`` at the
tool-call choke point), so a caller that skips this class and calls the runtime directly meets the same refusal."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Optional

from .audit import AuditSink, new_receipt
from .authz import Ceiling, allowed, perm


class ReplayRefused(Exception):
    def __init__(self, message: str = "replay operation not permitted"):
        super().__init__(message)


def _dispatches(events) -> int:
    return sum(1 for e in events if e.type in ("NodeDispatched", "NodeCompensated"))


def lineage_digest(events, upto: Optional[int] = None) -> str:
    """Hash chain over the mission's event history (type, seq, ts, payload, in order)."""
    h = hashlib.sha256(b"aos-lineage-v1")
    for e in list(events)[: upto if upto is not None else None]:
        h = hashlib.sha256(h.digest() + json.dumps(
            {"t": e.type, "s": getattr(e, "seq", 0), "ts": getattr(e, "ts", 0), "p": e.payload},
            sort_keys=True, separators=(",", ":"), default=str).encode())
    return "sha256:" + h.hexdigest()


class ReplayController:
    def __init__(self, runtime: Any, ceiling: Ceiling, audit: AuditSink, *, scope_for: Callable[[str], Optional[str]]):
        self.rt = runtime
        self.ceiling = ceiling
        self.audit = audit
        self.scope_for = scope_for

    # ── helpers ──
    def _scope(self, principal: Any, mission_id: str, action: str, kind: str) -> str:
        scope = None
        try:
            scope = self.scope_for(mission_id)
        except Exception:                                  # noqa: BLE001
            scope = None
        ok = False
        if scope:
            try:
                ok = allowed(self.ceiling, principal, perm(scope, action))
            except ValueError:
                ok = False
        if not ok:
            self.audit.append(new_receipt(kind, getattr(principal, "id", ""), str(scope or ""), mission_id=mission_id,
                                          outcome="refused", reason="not_authorized", side_effects=0))
            raise ReplayRefused()
        return scope

    def _events(self, mission_id: str):
        return self.rt.store.for_mission(mission_id)

    def _ensure_loaded(self, mission_id: str) -> None:
        if mission_id not in self.rt._missions:
            self.rt.rehydrate(mission_id)

    def _summary(self, mission_id: str) -> dict:
        ev = self._events(mission_id)
        m = self.rt._missions[mission_id]
        meta = self.rt._last_plan_meta(mission_id) or {}
        pend = self.rt.repo.pending_human(mission_id)
        return {"mission_id": mission_id, "state": m.state.name, "event_count": len(ev),
                "lineage_sha256": lineage_digest(ev), "plan_fingerprint": meta.get("plan_fingerprint", ""),
                "context_epoch_id": getattr(m, "context_epoch_id", ""), "intent_content_hash": getattr(m, "intent_content_hash", ""),
                "evidence_refs": list(getattr(m, "evidence_refs", []) or []), "pending_gate": pend}

    # ── operations ──
    def reconstruct(self, principal: Any, mission_id: str) -> dict:
        scope = self._scope(principal, mission_id, "reconstruct", "replay.reconstruct")
        before = _dispatches(self._events(mission_id)) if mission_id in self.rt._missions else 0
        self._ensure_loaded(mission_id)
        if not self.rt.is_reconstructed(mission_id):
            self.rt.rehydrate(mission_id)
        out = self._summary(mission_id)
        out["side_effects"] = _dispatches(self._events(mission_id)) - before
        self.audit.append(new_receipt("replay.reconstruct", principal.id, scope, mission_id=mission_id, outcome="ok", reason="",
                                      side_effects=out["side_effects"], lineage_sha256=out["lineage_sha256"],
                                      plan_fingerprint=out["plan_fingerprint"], state=out["state"]))
        return out

    def resume(self, principal: Any, mission_id: str) -> dict:
        scope = self._scope(principal, mission_id, "resume", "replay.resume")
        self._ensure_loaded(mission_id)
        if not self.rt.is_reconstructed(mission_id):
            self.rt.rehydrate(mission_id)
        self.rt.grant_resume(mission_id, principal=principal, ceiling=self.ceiling, scope=scope, action="resume")
        before = _dispatches(self._events(mission_id))
        self.rt.run(mission_id)
        out = self._summary(mission_id)
        out["side_effects"] = _dispatches(self._events(mission_id)) - before
        out["receipt"] = self.audit.append(new_receipt(
            "replay.resume", principal.id, scope, mission_id=mission_id, outcome="ok", reason="", side_effects=out["side_effects"],
            lineage_sha256=out["lineage_sha256"], plan_fingerprint=out["plan_fingerprint"], state=out["state"]))
        return out

    def reexecute(self, principal: Any, mission_id: str, *, cause: str, verified_intent: Any = None) -> dict:
        scope = self._scope(principal, mission_id, "reexecute", "replay.reexecute")
        self._ensure_loaded(mission_id)
        if not self.rt.is_reconstructed(mission_id):
            self.rt.rehydrate(mission_id)
        self.rt.grant_resume(mission_id, principal=principal, ceiling=self.ceiling, scope=scope, action="reexecute")
        before = _dispatches(self._events(mission_id))
        self.rt.re_evaluate(mission_id, verified_intent=verified_intent, cause=cause)
        self.rt.run(mission_id)
        out = self._summary(mission_id)
        meta = self.rt._last_plan_meta(mission_id) or {}
        out["revision"] = int(meta.get("revision", 1))
        out["side_effects"] = _dispatches(self._events(mission_id)) - before
        out["receipt"] = self.audit.append(new_receipt(
            "replay.reexecute", principal.id, scope, mission_id=mission_id, outcome="ok", reason=cause[:200],
            side_effects=out["side_effects"], lineage_sha256=out["lineage_sha256"],
            plan_fingerprint=out["plan_fingerprint"], state=out["state"], revision=out["revision"]))
        return out

    def verify_lineage(self, mission_id: str, *, expected: str, upto: int) -> bool:
        """True iff the first ``upto`` events still hash to ``expected`` (history unchanged since the checkpoint)."""
        self._ensure_loaded_readonly(mission_id)
        ev = self._events(mission_id)
        return len(ev) >= upto and lineage_digest(ev, upto) == expected

    def _ensure_loaded_readonly(self, mission_id: str) -> None:
        return None
