"""Phase 5 — approval inbox + undo window (plan §11).

The Phase 0 pipeline blocks a required-approval request as PENDING_APPROVAL via the ApprovalStore
seam (open-core default: nothing pre-approved). This adds the human side of that loop:

    request gated → an ApprovalRequest lands in an inbox → a person Approves / Rejects / Edits in
    the control plane / Sidekick → the agent retries the same request → it is now satisfied → it runs.

:class:`InboxApprovalStore` implements the seam with no gateway change: a first gated call records a
pending inbox item (keyed by the request's content-addressed identity) and returns not-satisfied; an
approved item makes the identical retry satisfied. For reversible actions, :class:`UndoWindow` holds
a compensation for a bounded time so an approved side effect can still be undone (the capability
declares its reverse via ``CapabilityManifest.compensation``).
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .contracts import CapabilityManifest, GatewayRequest


@dataclass
class ApprovalRequest:
    id: str
    fingerprint: str                 # request.intent_hash() — identical retries match
    capability: str
    subject: str
    tenant: str
    tier: int
    status: str = "pending"          # pending | approved | rejected
    decided_by: str = ""
    edit: Optional[dict] = None
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0          # 0 = never; set from the store's ttl when the item is created
    consumed: bool = False           # an approval satisfies exactly ONE execution

    def view(self) -> dict:
        return {"id": self.id, "capability": self.capability, "subject": self.subject,
                "tenant": self.tenant, "tier": self.tier, "status": self.status,
                "decided_by": self.decided_by, "expires_at": self.expires_at or None,
                "consumed": self.consumed}


class ApprovalError(PermissionError):
    """An approval decision that is not allowed (self-approval, expired, already decided)."""


@dataclass
class InboxApprovalStore:
    """An ApprovalStore backed by a human inbox. A gated request creates a pending item; a person
    approves/rejects it; the identical retry then satisfies the gate. Keyed by the request's
    content-addressed identity so approving one request never authorizes a different one.

    An approval is SINGLE-USE (the first satisfied retry consumes it; an identical later call parks a fresh
    item), expires after ``ttl`` seconds (0 = never), and may not be given by the requesting principal."""

    ttl: float = 900.0
    clock: Callable[[], float] = time.time
    _by_fp: Dict[str, ApprovalRequest] = field(default_factory=dict)
    _by_id: Dict[str, ApprovalRequest] = field(default_factory=dict)

    def _park(self, fp: str, request: GatewayRequest, manifest: CapabilityManifest) -> ApprovalRequest:
        now = self.clock()
        ar = ApprovalRequest(
            id="apr_" + secrets.token_urlsafe(10), fingerprint=fp, capability=request.capability,
            subject=request.principal.subject, tenant=request.principal.tenant,
            tier=int(manifest.risk_tier), created_at=now, expires_at=(now + self.ttl) if self.ttl else 0.0)
        self._by_fp[fp] = ar
        self._by_id[ar.id] = ar
        return ar

    def _expired(self, ar: ApprovalRequest) -> bool:
        return bool(ar.expires_at) and self.clock() >= ar.expires_at

    def is_satisfied(self, request: GatewayRequest, manifest: CapabilityManifest) -> bool:
        fp = request.intent_hash()
        ar = self._by_fp.get(fp)
        if ar is None or ar.consumed or (ar.status != "rejected" and self._expired(ar)):
            self._park(fp, request, manifest)   # new, used-up or lapsed → pending in the inbox again
            return False
        if ar.status == "approved":
            ar.consumed = True                  # single use: this execution spends the approval
            return True
        return False                            # rejected/pending ⇒ still not satisfied

    # ── control-plane / Sidekick inbox surface ──────────────────────────────────
    def pending(self) -> List[dict]:
        return [ar.view() for ar in self._by_id.values() if ar.status == "pending"]

    def get(self, approval_id: str) -> Optional[dict]:
        ar = self._by_id.get(approval_id)
        return ar.view() if ar else None

    def approve(self, approval_id: str, *, by: str, edit: Optional[dict] = None) -> dict:
        ar = self._require(approval_id)
        if not by:
            raise ApprovalError("an approval needs an identified approver")
        if by == ar.subject:
            raise ApprovalError("the requesting principal cannot approve its own request")
        if ar.consumed or ar.status != "pending":
            raise ApprovalError(f"approval {approval_id} is already {'used' if ar.consumed else ar.status}")
        if self._expired(ar):
            raise ApprovalError(f"approval {approval_id} has expired; the request must be raised again")
        ar.status, ar.decided_by, ar.edit = "approved", by, edit
        return ar.view()

    def reject(self, approval_id: str, *, by: str) -> dict:
        ar = self._require(approval_id)
        if ar.consumed:
            raise ApprovalError(f"approval {approval_id} was already used")
        ar.status, ar.decided_by = "rejected", by
        return ar.view()

    def _require(self, approval_id: str) -> ApprovalRequest:
        ar = self._by_id.get(approval_id)
        if ar is None:
            raise KeyError(f"no such approval: {approval_id}")
        return ar


@dataclass
class UndoEntry:
    id: str
    capability: str
    compensation: str                 # the reversing capability's name (declarative)
    expires_at: float
    by: str = ""
    undone: bool = False


@dataclass
class UndoWindow:
    """A bounded window in which an approved, reversible side effect can still be undone via its
    declared compensation (plan §11). The control plane records an entry after a successful
    reversible write and calls :meth:`undo` within the window; the compensation itself runs back
    through the governed gateway (it is a capability), so the undo is audited like any other call."""

    default_ttl: float = 300.0        # 5 minutes
    clock: Callable[[], float] = time.time
    _entries: Dict[str, UndoEntry] = field(default_factory=dict)
    _runners: Dict[str, Callable[[], object]] = field(default_factory=dict)

    def record(self, capability: str, compensation: str, run_compensation: Callable[[], object],
               *, ttl: Optional[float] = None, by: str = "") -> str:
        entry = UndoEntry(id="undo_" + secrets.token_urlsafe(8), capability=capability,
                          compensation=compensation,
                          expires_at=self.clock() + (ttl if ttl is not None else self.default_ttl),
                          by=by)
        self._entries[entry.id] = entry
        self._runners[entry.id] = run_compensation
        return entry.id

    def available(self) -> List[dict]:
        now = self.clock()
        return [{"id": e.id, "capability": e.capability, "compensation": e.compensation,
                 "expires_in": round(e.expires_at - now, 1)}
                for e in self._entries.values() if not e.undone and e.expires_at > now]

    def undo(self, entry_id: str, *, by: str = "") -> object:
        entry = self._entries.get(entry_id)
        if entry is None:
            raise KeyError(f"no such undo entry: {entry_id}")
        if entry.undone:
            raise ValueError("already undone")
        if entry.expires_at <= self.clock():
            raise ValueError("undo window has expired")
        result = self._runners[entry_id]()      # runs the compensation capability (governed)
        entry.undone, entry.by = True, by
        return result
