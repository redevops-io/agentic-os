"""Phase 3 — trusted approval bridge (plan §7, §11 Phase 3).

Provider permission is not ReDevOps authorization. An external agent may *display* an approval request,
but authorization happens only through a trusted ReDevOps surface and binds:

    verified principal · decision_id · the exact action (intent) digest · an attestation mechanism

This bridge turns a trusted approval into a projects :class:`~agentic_os.projects.contracts.Decision`
and hands the outbound operator its ``decision_id``. It enforces the Phase 3 acceptance criteria:

  * **prose claiming approval fails** — nothing an external agent says sets ``approved``; only
    :meth:`approve` (a ReDevOps-surface call) can, and it requires a principal + attestation;
  * **unverified approval cannot authorize** — an approval without an attestation mechanism is refused;
  * **replay fails** — an approval authorizes exactly one execution, then is consumed;
  * **post-approval mutation fails** — the approval binds the exact intent digest, so a changed request
    has a different digest and simply finds no approval (fail-closed).
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from agentic_os.overlays import Principal

from .contracts import AgentTaskRequest

# Lazy import inside methods: agentic_os.projects.__init__ pulls discovery_runtime (via
# workflow_learning); the approval bridge only needs projects.contracts.Decision when it actually
# mints a Decision, so importing it eagerly would couple the whole subpackage to that dependency.


class ApprovalError(RuntimeError):
    pass


@dataclass
class ExternalApproval:
    approval_id: str
    intent_digest: str
    capability: str
    request_id: str
    principal_id: str
    status: str = "pending"        # pending | approved | rejected
    decision_id: str = ""
    attestation: str = ""          # the mechanism that authenticated the human (e.g. webauthn/sso)
    consumed: bool = False
    decided_by: str = ""
    created_at: float = field(default_factory=time.time)

    def view(self) -> dict:
        return {"approval_id": self.approval_id, "capability": self.capability,
                "request_id": self.request_id, "principal_id": self.principal_id,
                "status": self.status, "decision_id": self.decision_id, "consumed": self.consumed}


@dataclass
class ExternalApprovalBridge:
    """Presents outbound requests for approval and ingests trusted approvals. In-memory; the control
    plane / Sidekick drive :meth:`approve` / :meth:`reject`, never the external agent."""

    _by_digest: Dict[str, ExternalApproval] = field(default_factory=dict)
    _by_id: Dict[str, ExternalApproval] = field(default_factory=dict)

    # ── presentation (safe to surface to the agent) ──────────────────────────────
    def present(self, request: AgentTaskRequest) -> str:
        """Record a pending approval for a request. The agent may see it exists; it cannot approve it."""
        digest = request.intent_digest()
        existing = self._by_digest.get(digest)
        if existing is not None and existing.status != "rejected":
            return existing.approval_id
        principal_id = request.identity.principal.id if request.identity.principal else ""
        ap = ExternalApproval(approval_id="xapr_" + secrets.token_urlsafe(10), intent_digest=digest,
                              capability=request.capability, request_id=request.request_id,
                              principal_id=principal_id)
        self._by_digest[digest] = ap
        self._by_id[ap.approval_id] = ap
        return ap.approval_id

    # ── trusted approval (ReDevOps surface only) ─────────────────────────────────
    def approve(self, approval_id: str, *, principal: Principal, by: str, attestation: str) -> "Decision":
        from agentic_os.projects.contracts import Decision  # noqa: PLC0415
        ap = self._require(approval_id)
        if not attestation:
            raise ApprovalError("unverified approval: an attestation mechanism is required")
        if not principal or not principal.id:
            raise ApprovalError("unverified approval: a verified principal is required")
        if ap.principal_id and principal.id != ap.principal_id:
            raise ApprovalError("principal mismatch: approver is not the bound principal")
        if ap.status == "approved":
            return Decision(request_id=ap.request_id, actor=by, action="approve",
                            selected=((ap.intent_digest, 0),), decision_id=ap.decision_id)
        decision = Decision(request_id=ap.request_id, actor=by, action="approve",
                            selected=((ap.intent_digest, 0),))
        ap.status, ap.decision_id, ap.attestation, ap.decided_by = (
            "approved", decision.decision_id, attestation, by)
        ap.principal_id = ap.principal_id or principal.id
        return decision

    def reject(self, approval_id: str, *, by: str) -> dict:
        ap = self._require(approval_id)
        ap.status, ap.decided_by = "rejected", by
        return ap.view()

    # ── the authorization check the operator path consults ───────────────────────
    def authorize(self, request: AgentTaskRequest, *, principal: Optional[Principal] = None) -> "Decision":
        """Return the bound Decision iff a trusted, unconsumed approval matches the EXACT request and
        principal. Consumes it (single-use) so a replay of the same request fails. Raises otherwise —
        fail-closed. A mutated request has a different digest and finds nothing here."""
        from agentic_os.projects.contracts import Decision  # noqa: PLC0415
        ap = self._by_digest.get(request.intent_digest())
        if ap is None or ap.status != "approved":
            raise ApprovalError("no trusted approval for this exact request")
        if ap.consumed:
            raise ApprovalError("approval already consumed (replay refused)")
        who = principal or request.identity.principal
        if ap.principal_id and (who is None or who.id != ap.principal_id):
            raise ApprovalError("principal mismatch on authorization")
        ap.consumed = True
        return Decision(request_id=ap.request_id, actor=ap.decided_by, action="approve",
                        selected=((ap.intent_digest, 0),), decision_id=ap.decision_id)

    def pending(self) -> list:
        return [ap.view() for ap in self._by_id.values() if ap.status == "pending"]

    def _require(self, approval_id: str) -> ExternalApproval:
        ap = self._by_id.get(approval_id)
        if ap is None:
            raise ApprovalError(f"no such approval: {approval_id}")
        return ap
