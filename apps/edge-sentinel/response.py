"""Governed response — propose → (Governance) approve → real operator executes → receipt → verify (Phase 7).

This is the first phase wired to the actual platform, and it deliberately owns as little as possible:

  * **Mission Runtime owns the action.** Edge Sentinel only *proposes* an :class:`ActionRequest` bound to a
    real, already-governed capability (``sentinel.block_ip`` etc.). It reads the approval semantics from the
    REAL ``agentic_os.mission.operator_sdk`` capability — it does not restate or reinvent them.
  * **Governance is the gate, not Edge Sentinel.** :func:`execute_response` refuses to run unless it is
    handed an *approved* decision; in production that decision comes from the Mission Runtime HumanTask the
    capability's ``approval_required`` triggers. A model here may draft and rank; it may never approve.
  * **Receipt ≠ verification.** The operator returns a receipt (it ran); verification is a SEPARATE,
    read-only step (:func:`verify_response`) that re-checks the world (the ban is actually present).
  * **CACAO is interop, not authority.** :func:`to_cacao`/:func:`from_cacao` import/export a proposal as a
    CACAO 2.0 playbook for exchange; they never execute.

Consuming the real operator contract (not a replica) is the whole point — the mapping breaks loudly if the
``sentinel.*`` capability shape changes, which is what keeps Edge Sentinel from quietly duplicating it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agentic_os.mission.operator_sdk import Capability, Operator

from .evidence import ActionReceipt, ActionRequest, ActionState, SecurityDecision, _now, sha256_hex


class GovernanceError(Exception):
    """Raised when Edge Sentinel is asked to execute a response that Governance has not authorized, or to
    propose a side-effecting response that is not approval-gated."""


def _capability(operator: Operator, name: str) -> Capability:
    for c in operator.manifest.capabilities:      # the Operator publishes its specs on its manifest
        if c.name == name:
            return c
    raise KeyError(f"operator '{operator.name}' has no capability '{name}'")


@dataclass(frozen=True)
class GovernedResponseProposal:
    """A proposed response, with approval semantics READ FROM the real capability (not asserted here)."""
    case_id: str
    finding_id: str
    capability_name: str
    inputs: dict
    approval_required: bool
    side_effecting: bool
    undo: str | None
    permissions: tuple[str, ...]

    @property
    def proposal_id(self) -> str:
        return f"resp-{sha256_hex(self.capability_name + '|' + repr(sorted(self.inputs.items())))[:16]}"

    def to_dict(self) -> dict:
        d = self.__dict__.copy(); d["proposal_id"] = self.proposal_id
        d["permissions"] = list(self.permissions); return d


def propose_response(operator: Operator, action_request: ActionRequest, *, case_id: str) -> GovernedResponseProposal:
    """Turn a Phase-1 ActionRequest into a governed proposal by binding it to the REAL operator capability
    and reading that capability's governance flags. Enforces the governed-response invariant: a
    side-effecting response MUST be approval-gated, or Edge Sentinel refuses to propose it."""
    cap = _capability(operator, action_request.capability)
    if cap.side_effecting and not cap.approval_required:
        raise GovernanceError(
            f"{cap.name} is side-effecting but not approval_required — refusing to propose an ungoverned action")
    return GovernedResponseProposal(
        case_id=case_id, finding_id=(action_request.finding_refs[0] if action_request.finding_refs else ""),
        capability_name=cap.name, inputs=dict(action_request.parameters),
        approval_required=cap.approval_required, side_effecting=cap.side_effecting,
        undo=cap.undo, permissions=tuple(cap.permissions))


def execute_response(operator: Operator, proposal: GovernedResponseProposal, *,
                     decision: SecurityDecision, idempotency_key: str = "") -> ActionReceipt:
    """Execute ONLY after an approved decision (Governance's, not Edge Sentinel's). Invokes the real
    operator capability and wraps the result as a Phase-1 ActionReceipt (execution proof, not verification)."""
    if proposal.approval_required and not (decision and decision.approved):
        raise GovernanceError(f"{proposal.capability_name} requires approval; no approved decision supplied")
    try:
        result = operator.invoke(proposal.capability_name, proposal.inputs,
                                 idempotency_key=idempotency_key or proposal.proposal_id)
        return ActionReceipt(request_id=proposal.proposal_id, decision_id=decision.decision_id,
                             status="SUCCEEDED", external_ref=str(result.get("id") or result.get("decision_id") or ""))
    except Exception as e:  # the operator/core failed — a FAILED receipt, still distinct from verification
        return ActionReceipt(request_id=proposal.proposal_id, decision_id=decision.decision_id,
                             status="FAILED", error=str(e))


@dataclass(frozen=True)
class Verification:
    """A SEPARATE, read-only confirmation that the action took effect. Not the receipt."""
    proposal_id: str
    verified: bool
    method: str
    detail: str = ""
    at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def verify_response(operator: Operator, proposal: GovernedResponseProposal, receipt: ActionReceipt) -> Verification:
    """Re-check the world with a READ-ONLY capability (sentinel.triage) — did the ban actually land?
    Distinct from the receipt: a SUCCEEDED receipt with an unverified world is a real, reportable state."""
    if receipt.status != "SUCCEEDED":
        return Verification(proposal.proposal_id, verified=False, method="none",
                            detail=f"no verification: receipt status {receipt.status}")
    ip = proposal.inputs.get("ip", "")
    try:
        posture = operator.invoke("sentinel.triage", {}) or {}
    except Exception as e:
        return Verification(proposal.proposal_id, verified=False, method="sentinel.triage", detail=str(e))
    blob = repr(posture)
    present = bool(ip) and ip in blob
    return Verification(proposal.proposal_id, verified=present, method="sentinel.triage",
                        detail=f"{'ban present' if present else 'ban NOT observed'} for {ip}")


# ──────────────────────────── CACAO interop (NOT execution authority) ────────────────────────────

def to_cacao(proposal: GovernedResponseProposal) -> dict:
    """Export a proposal as a CACAO 2.0 playbook for INTERCHANGE only. The action step is 'manual' and the
    approval requirement is carried through — importing this elsewhere never grants execution here."""
    action_id = f"action--{proposal.proposal_id}"
    return {
        "type": "playbook", "spec_version": "cacao-2.0",
        "name": f"Edge Sentinel response: {proposal.capability_name}",
        "description": "Interchange only — execution authority remains with Mission Runtime + Governance.",
        "created_by": "edge-sentinel", "workflow_start": "start--0",
        "x_edge_sentinel": {"approval_required": proposal.approval_required, "undo": proposal.undo,
                            "permissions": list(proposal.permissions)},
        "workflow": {
            "start--0": {"type": "start", "on_completion": action_id},
            action_id: {"type": "action", "name": proposal.capability_name,
                        "commands": [{"type": "manual",
                                      "command": f"{proposal.capability_name} {proposal.inputs}"}],
                        "agent": "individual--edge-sentinel", "on_completion": "end--0"},
            "end--0": {"type": "end"},
        },
    }


def from_cacao(playbook: dict) -> dict:
    """Parse a CACAO playbook back to {capability, inputs, approval_required} for interop. Does NOT execute
    and does NOT confer authority — a caller must still propose it through the governed path above."""
    wf = playbook.get("workflow", {})
    for step in wf.values():
        if step.get("type") == "action":
            cmd = (step.get("commands") or [{}])[0].get("command", "")
            cap, _, rest = cmd.partition(" ")
            return {"capability": step.get("name") or cap, "raw_inputs": rest,
                    "approval_required": playbook.get("x_edge_sentinel", {}).get("approval_required", True)}
    return {}
