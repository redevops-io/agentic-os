"""The canonical verified-transition kernel (self-learning doc §2).

Learn must consume **verified state transitions**, not intentions. A proposed action that is rejected,
that fails at the provider, or that cannot be verified must NOT evolve the state — and must NOT become an
Experience that attributes a later outcome to an action that never actually happened. Otherwise a
collections agent learns that "aggressive reminders work" from reminders that were never sent (§2.3).

One kernel, three invariants (§2.2):
  * INITIAL_STATE_ADMISSIBILITY      — is state_t valid?
  * ACTION_ADMISSIBILITY             — is the action permitted (approved where consequential) and valid?
  * TRAJECTORY_CONTRACT_CONSISTENCY  — did state_t+1 evolve exactly per ADMITTED execution + VERIFIED
                                       reality (a rejected/failed/unverified action leaves state unchanged)?

Production Missions, the simulation environment, historical replay, the benchmark harness, and the Learn
Experience builder should all delegate to THIS, rather than imitating the semantics independently.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from .decisions import DecisionContext, DecisionRecord, InterventionKind, Outcome
from .receipts import VerificationState, to_action_receipt, verify_step

# actions that are consequential (need approval + a receipt) vs internal decisions
_CONSEQUENTIAL = frozenset({
    InterventionKind.SOFT_REMINDER, InterventionKind.DIRECT_REMINDER, InterventionKind.PAYMENT_PLAN,
    InterventionKind.ESCALATION, InterventionKind.SERVICE_HOLD})
_INTERNAL = frozenset({InterventionKind.HOLD, InterventionKind.HUMAN_REVIEW})


class Admissibility(str, enum.Enum):
    ADMITTED = "admitted"
    REJECTED = "rejected"                 # a consequential action without approval — not executed
    INADMISSIBLE_STATE = "inadmissible_state"
    INADMISSIBLE_ACTION = "inadmissible_action"


@dataclass(frozen=True)
class AccountState:
    """The receivables state that must evolve under real execution + verification semantics."""
    customer_ref: str
    outstanding_cents: int
    reminders_sent: int = 0
    last_action: str = ""
    escalated: bool = False
    human_review_open: bool = False
    paid: bool = False

    def valid(self) -> bool:
        return self.outstanding_cents >= 0 and not (self.paid and self.outstanding_cents > 0)


@dataclass(frozen=True)
class TransitionResult:
    admissibility: Admissibility
    admitted: bool
    executed: bool                        # a real, VERIFIED state-changing execution occurred
    next_state: AccountState
    verification_state: VerificationState
    outcome_pending: bool                 # executed but the business outcome (payment) is observed later
    receipt: object = None                # canonical projects.ActionReceipt (None for internal/rejected)
    reason: str = ""


def _evolve(state: AccountState, action: InterventionKind) -> AccountState:
    """The ONLY place state changes for a verified consequential action."""
    if action in (InterventionKind.SOFT_REMINDER, InterventionKind.DIRECT_REMINDER):
        return replace(state, reminders_sent=state.reminders_sent + 1, last_action=action.value)
    if action in (InterventionKind.PAYMENT_PLAN, InterventionKind.ESCALATION):
        return replace(state, escalated=True, last_action=action.value)
    return replace(state, last_action=action.value)


def apply_transition(
    state_t: AccountState, action: InterventionKind, *, approved: bool = False, provider_ok: bool = False,
    reconciled: Optional[bool] = None, decision_id: str = "", provider_object_id: str = "",
    admissible_action: Optional[Callable[[InterventionKind], bool]] = None,
) -> TransitionResult:
    """Apply one decision to the state under real execution + verification semantics.

    - INITIAL_STATE_ADMISSIBILITY: an invalid state_t transitions nowhere.
    - HOLD / HUMAN_REVIEW: no execution, no receipt; state is unchanged (a valid, admitted no-op).
    - ACTION_ADMISSIBILITY: a consequential action WITHOUT approval is REJECTED — not executed, state
      unchanged (a rejected reminder is not a sent reminder).
    - TRAJECTORY_CONTRACT_CONSISTENCY: an approved+executed action evolves the state ONLY when
      verification confirms it (VERIFIED). Provider-ok-but-unverified (HELD/UNKNOWN) or a failed provider
      call leaves the state unchanged — the change is not admitted into the trajectory.
    """
    # 1. INITIAL_STATE_ADMISSIBILITY
    if not state_t.valid():
        return TransitionResult(Admissibility.INADMISSIBLE_STATE, False, False, state_t,
                                VerificationState.NOT_APPLICABLE, False, reason="initial state invalid")

    if admissible_action is not None and not admissible_action(action):
        return TransitionResult(Admissibility.INADMISSIBLE_ACTION, False, False, state_t,
                                VerificationState.NOT_APPLICABLE, False, reason="action not permitted")

    # internal decisions (do-nothing / route-to-human) never execute a provider action
    if action in _INTERNAL:
        return TransitionResult(Admissibility.ADMITTED, True, False,
                                replace(state_t, human_review_open=(action is InterventionKind.HUMAN_REVIEW)),
                                VerificationState.NOT_APPLICABLE, False, reason=f"{action.value}: no execution")

    # 2. ACTION_ADMISSIBILITY — a consequential action needs approval
    if action in _CONSEQUENTIAL and not approved:
        return TransitionResult(Admissibility.REJECTED, False, False, state_t,
                                VerificationState.NOT_APPLICABLE, False,
                                reason="consequential action not approved — rejected, state unchanged")

    receipt, v = to_action_receipt(capability=f"receivables.{action.value}", provider="channel",
                                   ok=provider_ok, provider_object_id=provider_object_id,
                                   reconciled=reconciled, decision_id=decision_id)

    # provider failed → nothing executed, state unchanged
    if not provider_ok:
        return TransitionResult(Admissibility.ADMITTED, True, False, state_t, v, False, receipt=receipt,
                                reason="execution failed at provider")

    # 3. TRAJECTORY_CONTRACT_CONSISTENCY — only a VERIFIED action evolves the state
    if v is VerificationState.VERIFIED:
        return TransitionResult(Admissibility.ADMITTED, True, True, _evolve(state_t, action), v, True,
                                receipt=receipt, reason="executed and verified")
    # provider ok but unverified (HELD/UNKNOWN/REFUTED) — do NOT admit the change into the trajectory
    return TransitionResult(Admissibility.ADMITTED, True, False, state_t, v, False, receipt=receipt,
                            reason=f"executed but unverified ({v.value}) — state not evolved")


# ── verified Experience (never a phantom action) ─────────────────────────────────────
@dataclass(frozen=True)
class VerifiedExperience:
    """(context → what was decided → what ACTUALLY happened → outcome). ``effective_action`` is the
    proposed action ONLY when it was admitted, executed AND verified; otherwise it is HOLD, because
    nothing actually happened. Learn attributes an outcome to ``effective_action`` — so a rejected or
    unverified reminder can never teach that reminders cause payment (§2.3)."""
    context_digest: str
    subject_ref: str
    proposed_action: str
    effective_action: str
    verified: bool
    verification_state: str
    outcome: Optional[Outcome] = None

    @property
    def experience_id(self) -> str:
        import hashlib
        return "vxp_" + hashlib.sha256(
            f"{self.context_digest}|{self.subject_ref}|{self.effective_action}".encode()).hexdigest()[:12]

    @property
    def proposal_was_executed(self) -> bool:
        """False when the proposed action did NOT actually, verifiably happen — in which case Learn
        credits any outcome to ``effective_action`` (HOLD), never to the phantom proposal (§2.3)."""
        return self.effective_action == self.proposed_action


def experience_from_transition(context: DecisionContext, decision: DecisionRecord,
                               result: TransitionResult, *,
                               outcome: Optional[Outcome] = None) -> VerifiedExperience:
    """Build a Learn-ready Experience that reflects VERIFIED reality, not the proposal."""
    proposed = decision.chosen.kind
    effective = proposed if (result.admitted and result.executed
                             and result.verification_state is VerificationState.VERIFIED) \
        else InterventionKind.HOLD.value
    return VerifiedExperience(
        context_digest=context.digest(), subject_ref=context.subject_ref, proposed_action=proposed,
        effective_action=effective, verified=result.executed and result.verification_state is VerificationState.VERIFIED,
        verification_state=result.verification_state.value, outcome=outcome)
