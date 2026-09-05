"""Governed install — the install executed as a saga over the mission event ledger.

P0.4's :func:`~.installer.install` stands modules up; this wraps it in the governance the rest
of the runtime guarantees, recorded in the same append-only event store (so it inherits the
DuckDB/Postgres durability from P0.5 and folds back on restart):

* **HITL approval** — if the plan has capabilities the device only permits under REVIEW
  (``plan.needs_approval``), the install parks at ``AWAITING_APPROVAL`` and does nothing until
  :func:`approve_install` records a decision. A rejected install never runs.
* **Saga / undo** — if execution does not fully succeed, the modules that were stood up are
  **torn down** (the deployer's compensation) and the install is recorded ``ROLLED_BACK`` — no
  half-installed device left behind.
* **Replay** — state is a fold over the events (:func:`install_status`), so a crashed/restarted
  install resumes at the same gate with the same decisions, and a completed install is idempotent.

This is the governance shape of a Mission (park → approve → execute → verify → compensate),
carried on the mission ledger; promoting it to a first-class MissionRuntime template (nodes +
operator) is a later step and changes none of these guarantees.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .installer import Deployer, InstallReceipt, default_has_credential, install
from .provisioning import InstallPlan
from .types import new_id
from .util import Fetch

# lifecycle events on the ledger, keyed by the install id (a mission-scoped stream)
REQUESTED = "InstallRequested"
AWAITING = "InstallAwaitingApproval"
APPROVED = "InstallApproved"
REJECTED = "InstallRejected"
COMPLETED = "InstallCompleted"
ROLLED_BACK = "InstallRolledBack"

# fold states
S_AWAITING = "AWAITING_APPROVAL"
S_APPROVED = "APPROVED"
S_REJECTED = "REJECTED"
S_COMPLETED = "COMPLETED"
S_ROLLED_BACK = "ROLLED_BACK"


class InstallNotApproved(RuntimeError):
    """execute_install was called while the install is parked awaiting a human decision."""


@dataclass(frozen=True)
class InstallRequest:
    install_id: str
    status: str
    needs_approval: tuple


def request_install(store, plan: InstallPlan, *, install_id: Optional[str] = None) -> InstallRequest:
    """Open a governed install. Parks at AWAITING_APPROVAL when the plan needs a human; otherwise
    it is auto-approved (nothing consequential is gated)."""
    install_id = install_id or new_id("install")
    store.append(REQUESTED, install_id, {"plan_id": plan.plan_id,
                                         "to_install": list(plan.to_install),
                                         "needs_approval": list(plan.needs_approval)})
    if plan.needs_approval:
        store.append(AWAITING, install_id, {"needs_approval": list(plan.needs_approval)})
        status = S_AWAITING
    else:
        store.append(APPROVED, install_id, {"actor": "auto", "reason": "no approval required"})
        status = S_APPROVED
    return InstallRequest(install_id=install_id, status=status,
                          needs_approval=tuple(plan.needs_approval))


def approve_install(store, install_id: str, *, decision: str = "approve", actor: str = "") -> str:
    """Record a human decision on a parked install. ``decision`` != "approve" rejects it."""
    if decision == "approve":
        store.append(APPROVED, install_id, {"actor": actor, "reason": "approved"})
        return S_APPROVED
    store.append(REJECTED, install_id, {"actor": actor, "decision": decision})
    return S_REJECTED


def install_status(store, install_id: str) -> str:
    """Fold the ledger to the install's current state. Terminal wins (completed/rolled back)."""
    status = ""
    for e in store.for_mission(install_id):
        t = e.type
        if t == REQUESTED and not status:
            status = "REQUESTED"
        elif t == AWAITING:
            status = S_AWAITING
        elif t == APPROVED:
            status = S_APPROVED
        elif t == REJECTED:
            status = S_REJECTED
        elif t == COMPLETED:
            status = S_COMPLETED
        elif t == ROLLED_BACK:
            status = S_ROLLED_BACK
    return status


def _completed_receipt_exists(store, install_id: str) -> bool:
    return any(e.type == COMPLETED for e in store.for_mission(install_id))


def execute_install(store, install_id: str, plan: InstallPlan, *, deployer: Deployer,
                    fetch: Optional[Fetch] = None,
                    has_credential: Callable[[str], bool] = default_has_credential,
                    provisioners: Optional[dict] = None, secret_dir: Optional[str] = None,
                    verify_timeout: float = 8.0) -> InstallReceipt:
    """Run the approved install as a saga. Refuses if not approved; tears down + records
    ROLLED_BACK if it does not fully succeed; idempotent once COMPLETED."""
    status = install_status(store, install_id)
    if status == S_COMPLETED:
        raise InstallAlreadyDone(install_id)     # caller should read the recorded receipt
    if status != S_APPROVED:
        raise InstallNotApproved(f"install {install_id} is {status or 'unknown'}, not approved")

    receipt = install(plan, deployer=deployer, store=store, fetch=fetch,
                      has_credential=has_credential, provisioners=provisioners,
                      secret_dir=secret_dir, scope=install_id, verify_timeout=verify_timeout)

    if receipt.ok:
        store.append(COMPLETED, install_id, {"receipt_id": receipt.receipt_id,
                                             "modules": sorted(receipt.modules_yaml),
                                             "notices": list(receipt.notices)})
        return receipt

    # saga compensation: tear down every module that was actually stood up (has an endpoint)
    torn = []
    for o in receipt.installed:
        if o.base_url:
            try:
                deployer.teardown(o.module)
                torn.append(o.module)
            except Exception:      # compensation is best-effort; the failure is still recorded
                pass
    failed = [o.module for o in receipt.installed if not o.verified]
    store.append(ROLLED_BACK, install_id, {"failed": failed, "torn_down": torn,
                                           "receipt_id": receipt.receipt_id})
    return receipt


class InstallAlreadyDone(RuntimeError):
    """execute_install was called on an install already recorded COMPLETED (idempotency guard)."""
