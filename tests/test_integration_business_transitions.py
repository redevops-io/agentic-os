"""The verified-transition kernel (self-learning doc §2): rejected/failed/unverified actions never evolve
state or produce a phantom Experience."""
from __future__ import annotations

from agentic_os.integrations.business import (
    AccountState, Admissibility, InterventionKind as K, Outcome, VerificationState, apply_transition,
    experience_from_transition)
from agentic_os.integrations.business.decisions import (
    AccountFeatures, Candidate, DecisionContext, DecisionPoint, DecisionRecord)


def _state(**kw):
    base = dict(customer_ref="c1", outstanding_cents=500_00)
    base.update(kw)
    return AccountState(**base)


# ── invariant 1: initial-state admissibility ─────────────────────────────────────────
def test_invalid_initial_state_transitions_nowhere():
    bad = AccountState(customer_ref="c1", outstanding_cents=-1)
    r = apply_transition(bad, K.DIRECT_REMINDER, approved=True, provider_ok=True, reconciled=True)
    assert r.admissibility is Admissibility.INADMISSIBLE_STATE and r.next_state is bad and not r.executed


# ── invariant 2: action admissibility — rejected ≠ executed ──────────────────────────
def test_consequential_action_without_approval_is_rejected_and_state_unchanged():
    s = _state(reminders_sent=1)
    r = apply_transition(s, K.DIRECT_REMINDER, approved=False, provider_ok=True, reconciled=True)
    assert r.admissibility is Admissibility.REJECTED and not r.executed
    assert r.next_state == s and r.next_state.reminders_sent == 1     # NOT incremented
    assert r.receipt is None


def test_hold_and_human_review_are_admitted_no_ops():
    s = _state()
    rh = apply_transition(s, K.HOLD, approved=False)
    assert rh.admissibility is Admissibility.ADMITTED and not rh.executed and rh.next_state == s
    rr = apply_transition(s, K.HUMAN_REVIEW, approved=False)
    assert rr.next_state.human_review_open is True and not rr.executed and rr.receipt is None


# ── invariant 3: trajectory consistency — only VERIFIED evolves state ────────────────
def test_verified_execution_evolves_state_and_emits_receipt():
    s = _state(reminders_sent=1)
    r = apply_transition(s, K.DIRECT_REMINDER, approved=True, provider_ok=True, reconciled=True,
                         decision_id="dec-1", provider_object_id="MSG1")
    assert r.executed and r.next_state.reminders_sent == 2 and r.outcome_pending
    assert r.receipt.status == "SUCCEEDED" and r.receipt.decision_id == "dec-1"


def test_provider_ok_but_unverified_does_not_evolve_state():
    s = _state(reminders_sent=1)
    # provider accepted the send, but re-observation did not confirm it (REFUTED) → not admitted
    r = apply_transition(s, K.DIRECT_REMINDER, approved=True, provider_ok=True, reconciled=False)
    assert not r.executed and r.next_state == s and r.next_state.reminders_sent == 1
    assert r.receipt.status == "HELD" and r.verification_state is VerificationState.REFUTED
    # unobservable (UNKNOWN) likewise leaves the trajectory unchanged
    r2 = apply_transition(s, K.DIRECT_REMINDER, approved=True, provider_ok=True, reconciled=None)
    assert not r2.executed and r2.next_state == s and r2.verification_state is VerificationState.UNKNOWN


def test_failed_provider_call_does_not_evolve_state():
    s = _state()
    r = apply_transition(s, K.ESCALATION, approved=True, provider_ok=False)
    assert not r.executed and r.next_state == s and not r.next_state.escalated
    assert r.receipt.status == "FAILED"


# ── §2.3: Learn never attributes an outcome to an action that did not happen ─────────
def _context():
    f = AccountFeatures(subject_ref="c1", amount_outstanding_cents=500_00, currency="usd", days_overdue=40,
                        has_contact=True, prior_reminders=1, promise_to_pay=False, promise_recent=False,
                        open_dispute=False, strategic=False, cost_of_intervention_cents=200,
                        materiality_cents=500_00 * 40)
    return DecisionContext(DecisionPoint.INTERVENE_OR_HOLD, "c1", f)


def _decision(kind):
    return DecisionRecord(DecisionPoint.INTERVENE_OR_HOLD, "c1", Candidate(kind), "arm", "ctx")


def test_rejected_reminder_then_payment_does_not_teach_that_reminders_work():
    s = _state()
    # a direct reminder is PROPOSED but rejected (not approved); the customer later pays anyway
    result = apply_transition(s, K.DIRECT_REMINDER, approved=False, provider_ok=True, reconciled=True)
    outcome = Outcome(subject_ref="c1", decision_id="d", paid=True, days_to_pay=6, amount_recovered_cents=500_00)
    xp = experience_from_transition(_context(), _decision(K.DIRECT_REMINDER.value), result, outcome=outcome)
    # the phantom reminder is NOT credited — the effective action is HOLD, so Learn sees "hold → paid"
    assert xp.proposed_action == K.DIRECT_REMINDER.value
    assert xp.effective_action == K.HOLD.value and not xp.proposal_was_executed and not xp.verified


def test_verified_reminder_then_payment_is_a_real_experience():
    s = _state()
    result = apply_transition(s, K.DIRECT_REMINDER, approved=True, provider_ok=True, reconciled=True)
    outcome = Outcome(subject_ref="c1", decision_id="d", paid=True, days_to_pay=3)
    xp = experience_from_transition(_context(), _decision(K.DIRECT_REMINDER.value), result, outcome=outcome)
    assert xp.effective_action == K.DIRECT_REMINDER.value and xp.proposal_was_executed and xp.verified
