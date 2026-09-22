"""Phase D — Receivables decision-learning instrumentation.

The load-bearing decision is *should we intervene?* — HOLD/"do nothing yet" is a first-class candidate,
and the deterministic model is the CONTROL ARM (a swappable strategy), not the product.
"""
from __future__ import annotations

from agentic_os.integrations.business import (
    Candidate, CandidateSet, Contact, DecisionContext, DecisionPoint, DeterministicReceivablesModel,
    InterventionKind, Message, Provenance, Receivable, ReceivablesDecisionTrail, Ticket,
    decide_receivable, features_for, investigate_receivables)


def _decide(evidence, **kw):
    cands = investigate_receivables(evidence)
    return {c.customer_key: decide_receivable(c, evidence=evidence, **kw) for c in cands}


def _receivable(ref, cents, days):
    return Receivable(prov=Provenance("quickbooks", ref), invoice_ref="INV", customer_ref=ref,
                      amount_outstanding_cents=cents, currency="usd", days_overdue=days)


# ── the load-bearing decision: intervene vs HOLD, for real reasons ───────────────────
def test_recent_promise_to_pay_chooses_hold_not_a_reminder():
    ev = [_receivable("cust-acme", 300000, 15),
          Contact(prov=Provenance("hubspot", "cust-acme"), email="ap@acme.com", first_name="Dana"),
          Message(prov=Provenance("gmail", "m1", known_at=0), channel="email", thread_ref="cust-acme",
                  direction="inbound", from_ref="ap@acme.com", snippet="thanks — we will pay next week")]
    trail = _decide(ev)["cust-acme"]
    assert trail.held is True
    assert trail.intervene_or_hold.chosen.kind == InterventionKind.HOLD.value
    assert trail.selected_intervention is None          # no intervention selected when holding
    assert "promise" in trail.intervene_or_hold.rationale


def test_open_dispute_routes_to_human_review_not_auto_intervention():
    ev = [_receivable("cust-b", 400000, 45),
          Contact(prov=Provenance("hubspot", "cust-b"), email="bo@b.co"),
          Ticket(prov=Provenance("zendesk", "t1"), subject="wrong invoice amount", status="open",
                 requester_ref="bo@b.co")]
    trail = _decide(ev)["cust-b"]
    assert trail.held is True and trail.intervene_or_hold.chosen.kind == InterventionKind.HUMAN_REVIEW.value


def test_immaterial_never_nudged_holds():
    ev = [_receivable("cust-e", 5000, 10),
          Contact(prov=Provenance("hubspot", "cust-e"), email="eo@e.co")]
    trail = _decide(ev)["cust-e"]
    assert trail.intervene_or_hold.chosen.kind == InterventionKind.HOLD.value


# ── when intervention IS warranted, the right one is chosen ──────────────────────────
def test_ignored_after_multiple_reminders_escalates():
    ev = [_receivable("cust-c", 250000, 70),
          Contact(prov=Provenance("hubspot", "cust-c"), email="co@c.co"),
          Message(prov=Provenance("gmail", "r1"), channel="email", thread_ref="cust-c",
                  direction="outbound", to_refs=("co@c.co",), snippet="reminder 1"),
          Message(prov=Provenance("gmail", "r2"), channel="email", thread_ref="cust-c",
                  direction="outbound", to_refs=("co@c.co",), snippet="reminder 2")]
    trail = _decide(ev)["cust-c"]
    assert trail.held is False
    assert trail.selected_intervention == InterventionKind.ESCALATION.value


def test_fresh_material_overdue_gets_a_soft_reminder():
    ev = [_receivable("cust-d", 150000, 20),
          Contact(prov=Provenance("hubspot", "cust-d"), email="do@d.co")]
    trail = _decide(ev)["cust-d"]
    assert trail.held is False and trail.selected_intervention == InterventionKind.SOFT_REMINDER.value


# ── "do nothing" is structurally guaranteed; features are evidence-grounded ──────────
def test_hold_is_always_a_candidate_and_features_are_derived_from_evidence():
    from agentic_os.integrations.business.decisions import intervene_or_hold_candidates
    assert InterventionKind.HOLD.value in intervene_or_hold_candidates().kinds()
    ev = [_receivable("cust-c", 250000, 70),
          Contact(prov=Provenance("hubspot", "cust-c"), email="co@c.co"),
          Message(prov=Provenance("gmail", "r1"), channel="email", thread_ref="cust-c",
                  direction="outbound", to_refs=("co@c.co",), snippet="reminder 1")]
    cand = investigate_receivables(ev)[0]
    f = features_for(cand, evidence=ev)
    assert f.prior_reminders == 1 and f.has_contact is True and f.open_dispute is False
    assert f.days_overdue == 70 and f.materiality_cents == 250000 * 70


# ── the strategy is swappable — the whole point of Phase E ──────────────────────────
def test_strategy_is_swappable_a_different_model_decides_differently():
    class _AlwaysIntervene:
        strategy_id = "aggressive-arm"
        def propose(self, context: DecisionContext, candidates: CandidateSet):
            from agentic_os.integrations.business import DecisionProposal
            rec = next((c for c in candidates.candidates if c.kind == "intervene"), candidates.candidates[0])
            return DecisionProposal(context_digest=context.digest(), recommended=rec, alternatives=(),
                                    rationale="always act", strategy_id=self.strategy_id)

    ev = [_receivable("cust-acme", 300000, 15),
          Contact(prov=Provenance("hubspot", "cust-acme"), email="ap@acme.com"),
          Message(prov=Provenance("gmail", "m1", known_at=0), channel="email", thread_ref="cust-acme",
                  direction="inbound", from_ref="ap@acme.com", snippet="we will pay next week")]
    cand = investigate_receivables(ev)[0]
    # control arm HOLDs on the promise; the aggressive arm intervenes on the SAME evidence
    control = decide_receivable(cand, evidence=ev, model=DeterministicReceivablesModel())
    aggressive = decide_receivable(cand, evidence=ev, model=_AlwaysIntervene())
    assert control.held is True and aggressive.held is False
    assert control.records[0].strategy_id == "deterministic-control"
    assert aggressive.records[0].strategy_id == "aggressive-arm"


def test_decision_records_are_content_addressed_and_carry_provenance():
    ev = [_receivable("cust-d", 150000, 20), Contact(prov=Provenance("hubspot", "cust-d"), email="do@d.co")]
    trail = _decide(ev)["cust-d"]
    rec = trail.intervene_or_hold
    assert rec.decision_id.startswith("bdec_") and rec.context_digest.startswith("sha256:")
    d = rec.to_dict()
    assert d["point"] == "intervene_or_hold" and d["strategy_id"] == "deterministic-control"
