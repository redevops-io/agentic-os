"""Support-autonomy primitives — opt-out/STOP, Risk-Radar follow-up, lead scoring, self-improving
KB, and human-handoff. Deterministic, clock-injectable; no live services."""
from __future__ import annotations

from agentic_os.support_autonomy import (
    FollowUpPolicy, HandoffDecision, OptOutDetector, SelfImprovingKB, ThreadRisk, ThreadState,
    handoff_decision, qualify_lead)


class _Clock:
    def __init__(self, t=1_000_000.0): self.t = t
    def __call__(self): return self.t
    def advance(self, s): self.t += s


# ── opt-out / STOP ─────────────────────────────────────────────────────────────────
def test_bare_stop_word_on_a_short_message_opts_out():
    d = OptOutDetector()
    assert d.detect("STOP").opted_out
    assert d.detect("parar").opted_out                 # PT
    assert d.detect("baja").opted_out                  # ES


def test_opt_out_phrase_matches_anywhere():
    d = OptOutDetector()
    assert d.detect("please remove me from this list").opted_out
    assert d.detect("por favor, não quero receber mais mensagens").opted_out


def test_bare_stop_word_inside_a_long_message_does_not_false_positive():
    d = OptOutDetector()
    assert not d.detect("I'll meet you at the bus stop near the office at noon").opted_out
    assert not d.detect("can you quote me a price for the annual plan?").opted_out


# ── Risk-Radar / follow-up ──────────────────────────────────────────────────────────
def test_follow_up_becomes_due_after_the_first_interval():
    clk = _Clock()
    pol = FollowUpPolicy(intervals=(3600, 86400, 259200), clock=clk)
    ts = ThreadState(last_inbound_at=clk.t, status="awaiting_customer", follow_ups_sent=0)
    assert pol.assess(ts).should_follow_up is False    # just now → not due
    clk.advance(3700)                                  # past 1h
    d = pol.assess(ts)
    assert d.should_follow_up and d.follow_up_number == 1 and d.risk is ThreadRisk.COOLING


def test_follow_up_caps_and_marks_at_risk_then_lost():
    clk = _Clock()
    pol = FollowUpPolicy(intervals=(3600, 86400), lost_after=1209600, clock=clk)
    ts = ThreadState(last_inbound_at=clk.t, status="awaiting_customer", follow_ups_sent=2)
    assert pol.assess(ts).should_follow_up is False    # cap reached (2 of 2 sent)
    clk.advance(2_000_000)                             # very idle
    assert pol.assess(ts).risk is ThreadRisk.LOST


def test_no_follow_up_when_resolved_or_opted_out():
    pol = FollowUpPolicy(clock=_Clock())
    assert not pol.assess(ThreadState(status="resolved")).should_follow_up
    assert not pol.assess(ThreadState(status="awaiting_customer", opted_out=True)).should_follow_up


# ── lead scoring ────────────────────────────────────────────────────────────────────
def test_buying_intent_plus_contact_details_is_a_hot_lead():
    s = qualify_lead(message="what's your pricing for the pro plan?", has_email=True,
                     has_company=True, message_count=4)
    assert s.tier == "P0" and "explicit buying intent" in s.reasons


def test_a_bare_greeting_is_cold():
    assert qualify_lead(message="hi", message_count=1).tier == "P3"


# ── self-improving KB ─────────────────────────────────────────────────────────────
def test_a_resolution_answers_the_next_identical_question():
    kb = SelfImprovingKB()
    kb.learn_from_resolution("how do I reset my password?",
                             "Open Settings → Security → Reset password.")
    hit = kb.recall("i forgot my password, how to reset it?")
    assert hit is not None and "Settings" in hit.answer


def test_recall_misses_when_nothing_is_relevant():
    kb = SelfImprovingKB()
    kb.learn("refund policy?", "30 days.")
    assert kb.recall("what are your office hours") is None


def test_learned_entries_can_persist_through_the_sink():
    seen = []
    kb = SelfImprovingKB(persist=seen.append)
    kb.learn("q", "a")
    assert len(seen) == 1 and seen[0].answer == "a"


# ── handoff ─────────────────────────────────────────────────────────────────────────
def test_explicit_request_and_negative_sentiment_escalate():
    assert handoff_decision(explicit_request=True).handoff
    assert handoff_decision(sentiment=-0.8).handoff


def test_low_confidence_on_at_risk_escalates_but_confident_does_not():
    assert handoff_decision(agent_confident=False, risk=ThreadRisk.AT_RISK).handoff
    assert not handoff_decision(agent_confident=True, risk=ThreadRisk.AT_RISK).handoff
    assert not handoff_decision(sentiment=0.2).handoff
