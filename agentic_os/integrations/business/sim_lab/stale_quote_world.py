"""World 2 — Stale-quote follow-up (plan §5, Contractor family).

A contractor sent a quote; it is now stale (days elapsed, not yet signed). The decision is what to do to
convert it — or to recognise it is not ready (HOLD), needs a different tack (OFFER_ALTERNATIVE /
REQUEST_TIMELINE), or is dead (CLOSE_LOST). This is the same *decision structure* as receivables follow-up
(intervention timing under uncertainty, with a real cost to over-intervening) but a different commercial
task and a different, richer action vocabulary — so it is the natural test of whether the receivables
lesson generalises or was collections-specific.

Two design commitments the reframe requires:

1. **Domain-native actions**, not receivables' verbs:
   HOLD · SOFT_FOLLOWUP · DIRECT_FOLLOWUP · CALL · REQUEST_TIMELINE · OFFER_ALTERNATIVE · HUMAN_REVIEW ·
   CLOSE_LOST. Two of these (REQUEST_TIMELINE, CLOSE_LOST) have no receivables analogue — a transferred
   posture policy literally cannot emit them, which caps how far transfer alone can go.

2. **A long-horizon oracle.** ``net_value`` is the *eventual* expected utility of the deal given the action
   — conversion margin realised later, minus cost — NOT whether the customer replies now. Over-touching a
   touch-sensitive prospect can raise the reply rate while lowering eventual conversion (fatigue), so an arm
   that chases activity is penalised. That is the behaviour the Runtime is meant to improve, so the naive
   baseline here is deliberately an activity-driven sales cadence.
"""
from __future__ import annotations

import random
from typing import Any, Mapping, Sequence

from .abstract import (
    AbstractFeatures, Posture, Readiness, Stakes, elapsed_of, fatigue_of)
from .harness import CaseOutcome, DecisionCase

HOLD = "hold"
SOFT_FOLLOWUP = "soft_followup"
DIRECT_FOLLOWUP = "direct_followup"
CALL = "call"
REQUEST_TIMELINE = "request_timeline"
OFFER_ALTERNATIVE = "offer_alternative"
HUMAN_REVIEW = "human_review"
CLOSE_LOST = "close_lost"

_ALL = (HOLD, SOFT_FOLLOWUP, DIRECT_FOLLOWUP, CALL, REQUEST_TIMELINE, OFFER_ALTERNATIVE,
        HUMAN_REVIEW, CLOSE_LOST)
_CONTACT = frozenset({SOFT_FOLLOWUP, DIRECT_FOLLOWUP, CALL, REQUEST_TIMELINE, OFFER_ALTERNATIVE,
                      HUMAN_REVIEW})   # actions that reach out (subject to fatigue); HOLD/CLOSE_LOST do not

OBSERVABLE_KEYS = frozenset({
    "days_since_quote", "prior_touches", "last_signal", "quote_value_band", "competing_bid_flag"})

# ── long-horizon economics ──────────────────────────────────────────────────────────
_MARGIN_RATE = 0.22
_COST = {                       # cents; rep/manager time and channel cost
    HOLD: 0, SOFT_FOLLOWUP: 150, DIRECT_FOLLOWUP: 200, CALL: 1_200, REQUEST_TIMELINE: 200,
    OFFER_ALTERNATIVE: 250, HUMAN_REVIEW: 1_500, CLOSE_LOST: 0}
_CONCESSION_KEEP = 0.75         # OFFER_ALTERNATIVE wins the deal but at 75% of the margin
_DEAD_HOLD_DRAG = 600           # holding a dead lead consumes pipeline attention

# eventual conversion probability by true segment × action, BEFORE fatigue is applied.
_CONVERT = {
    "ready":      {HOLD: 0.55, SOFT_FOLLOWUP: 0.70, DIRECT_FOLLOWUP: 0.82, CALL: 0.88,
                   REQUEST_TIMELINE: 0.68, OFFER_ALTERNATIVE: 0.72, HUMAN_REVIEW: 0.80, CLOSE_LOST: 0.0},
    "needs_time": {HOLD: 0.60, SOFT_FOLLOWUP: 0.62, DIRECT_FOLLOWUP: 0.42, CALL: 0.38,
                   REQUEST_TIMELINE: 0.66, OFFER_ALTERNATIVE: 0.50, HUMAN_REVIEW: 0.55, CLOSE_LOST: 0.0},
    "comparison": {HOLD: 0.35, SOFT_FOLLOWUP: 0.42, DIRECT_FOLLOWUP: 0.45, CALL: 0.52,
                   REQUEST_TIMELINE: 0.55, OFFER_ALTERNATIVE: 0.72, HUMAN_REVIEW: 0.60, CLOSE_LOST: 0.0},
    # truly dead: no action converts. CLOSE_LOST (0 cost, frees the pipeline) is optimal — but a dead lead
    # presents as "silent", the same signal a patient (needs_time) lead gives, so recovering CLOSE_LOST is
    # an observability problem, not a policy one. That limit is a finding, not a bug.
    "dead":       {HOLD: 0.0, SOFT_FOLLOWUP: 0.0, DIRECT_FOLLOWUP: 0.0, CALL: 0.0,
                   REQUEST_TIMELINE: 0.0, OFFER_ALTERNATIVE: 0.0, HUMAN_REVIEW: 0.0, CLOSE_LOST: 0.0},
}
_SEGMENTS = ("ready", "needs_time", "comparison", "dead")
# how a segment reveals itself in the observable last_signal (noisy — this is the readiness evidence)
_SIGNAL_P = {
    "ready":      [("replied_question", 0.72), ("opened_no_reply", 0.18), ("silent", 0.07), ("price_objection", 0.03)],
    "needs_time": [("silent", 0.58), ("opened_no_reply", 0.30), ("replied_question", 0.08), ("price_objection", 0.04)],
    "comparison": [("price_objection", 0.68), ("replied_question", 0.16), ("opened_no_reply", 0.10), ("silent", 0.06)],
    # a dead lead looks the same as a patient one — silence. This overlap with needs_time is deliberate:
    # it is why CLOSE_LOST cannot be reliably recovered from the signal alone (an observability limit).
    "dead":       [("silent", 0.66), ("opened_no_reply", 0.24), ("replied_question", 0.05), ("price_objection", 0.05)],
}


class StaleQuoteWorld:
    world_id = "stale_quote_followup/v1"

    def actions(self) -> tuple[str, ...]:
        return _ALL

    def hold_actions(self) -> frozenset[str]:
        return frozenset({HOLD})

    def default_hold(self) -> str:
        return HOLD

    # ── oracle (reads latent only): eventual expected utility, not immediate response ──
    def net_value(self, latent: Mapping[str, Any], action: str) -> float:
        seg = latent["segment"]
        margin = latent["quote_value_cents"] * _MARGIN_RATE
        conv = _CONVERT[seg][action]
        if action in _CONTACT:
            # fatigue: another touch on an already-worked, touch-sensitive prospect erodes eventual conversion
            fatigue = max(0.2, 1.0 - latent["touch_sensitivity"] * latent["prior_touches"] * 0.22)
            conv *= fatigue
        realized_margin = margin * (_CONCESSION_KEEP if action == OFFER_ALTERNATIVE else 1.0)
        value = conv * realized_margin - _COST[action]
        if action == HOLD and seg == "dead":
            value -= _DEAD_HOLD_DRAG
        return value

    def optimal(self, latent: Mapping[str, Any]) -> str:
        return max(_ALL, key=lambda a: self.net_value(latent, a))

    # ── feasibility (observable only) ──
    def admissible_action(self, observable: Mapping[str, Any], action: str) -> bool:
        if action == HUMAN_REVIEW:      # a sales manager is scarce: only high-value or contested deals
            return bool(observable.get("quote_value_band") in ("l", "xl")
                        or observable.get("competing_bid_flag"))
        return True

    # ── learning key (native) ──
    # deliberately excludes days_since_quote: it does not enter the oracle (a distractor), so a good learner
    # keys on the readiness signal, stakes, competition, and prior-touch fatigue instead.
    def bucket(self, observable: Mapping[str, Any]) -> str:
        return (f"{observable.get('quote_value_band','?')}:{observable.get('last_signal','?')}:"
                f"t{min(int(observable.get('prior_touches', 0)), 3)}:"
                f"{'cmp' if observable.get('competing_bid_flag') else 'nc'}")

    # ── arm A baseline: an activity-driven sales cadence (the behaviour we want to improve) ──
    def baseline_action(self, observable: Mapping[str, Any]) -> str:
        touches = int(observable.get("prior_touches", 0))
        if touches == 0:
            return SOFT_FOLLOWUP
        if touches == 1:
            return DIRECT_FOLLOWUP
        return CALL                      # keep escalating the touch — never rests, never disengages

    # ── the abstract-feature view (transfer vehicle) — observable only ──
    def abstract_features(self, observable: Mapping[str, Any]) -> AbstractFeatures:
        sig = observable.get("last_signal")
        readiness = (Readiness.POSITIVE if sig == "replied_question"
                     else Readiness.NEUTRAL)   # sales rarely yields an explicit "never" signal; dead looks silent
        stakes = Stakes.HIGH if observable.get("quote_value_band") in ("l", "xl") else Stakes.LOW
        return AbstractFeatures(
            readiness=readiness, elapsed=elapsed_of(int(observable.get("days_since_quote", 0))),
            fatigue=fatigue_of(int(observable.get("prior_touches", 0))), stakes=stakes, contested=False)

    # ── posture → native action bridge (how a transferred posture becomes a stale-quote action) ──
    def action_for_posture(self, posture: Posture, observable: Mapping[str, Any]) -> str:
        stakes_high = observable.get("quote_value_band") in ("l", "xl")
        return {
            Posture.HOLD: HOLD,
            Posture.GENTLE: SOFT_FOLLOWUP,
            Posture.DECISIVE: CALL if stakes_high else DIRECT_FOLLOWUP,
            Posture.CONCESSION: OFFER_ALTERNATIVE,
            Posture.ELICIT: REQUEST_TIMELINE,
            Posture.HUMAN: HUMAN_REVIEW if self.admissible_action(observable, HUMAN_REVIEW) else DIRECT_FOLLOWUP,
            Posture.CONTESTED: HUMAN_REVIEW if self.admissible_action(observable, HUMAN_REVIEW) else DIRECT_FOLLOWUP,
            Posture.DISENGAGE: CLOSE_LOST,
        }[posture]

    # ── corpus ──
    def cases(self, *, seed: int, n: int, shift: float = 0.0) -> tuple[DecisionCase, ...]:
        rng = random.Random(seed)
        weights = [0.28, 0.30, 0.22, 0.20]
        if shift:                                   # covariate shift: more dead + comparison leads at eval
            weights = [max(0.05, 0.28 - shift), max(0.05, 0.30 - shift),
                       0.22 + shift, 0.20 + shift]
        out: list[DecisionCase] = []
        for i in range(n):
            seg = rng.choices(_SEGMENTS, weights)[0]
            value = rng.choice([4_000_00, 12_000_00, 28_000_00, 60_000_00])  # $4k–$60k jobs, in cents
            band = ("xl" if value >= 60_000_00 else "l" if value >= 28_000_00 else
                    "m" if value >= 12_000_00 else "s")
            days = rng.choice([8, 20, 35, 55, 80])
            prior = rng.choice([0, 0, 1, 2, 3, 4])
            sig = rng.choices([s for s, _ in _SIGNAL_P[seg]], [p for _, p in _SIGNAL_P[seg]])[0]
            observable = {
                "days_since_quote": days, "prior_touches": prior, "last_signal": sig,
                "quote_value_band": band,
                "competing_bid_flag": (seg == "comparison" and rng.random() < 0.7) or rng.random() < 0.1}
            latent = {"segment": seg, "quote_value_cents": value,
                      "touch_sensitivity": round(rng.uniform(0.2, 1.0), 2), "prior_touches": prior}
            out.append(DecisionCase(
                case_id=f"{self.world_id}:{seed}:{i}", world_id=self.world_id,
                observable=observable, latent=latent, known_at=i))
        return tuple(out)

    # ── operational metrics ──
    def extra_metrics(self, outcomes: Sequence[CaseOutcome],
                      cases: Sequence[DecisionCase]) -> Mapping[str, float]:
        by_id = {c.case_id: c for c in cases}
        n = len(outcomes) or 1
        dead = [o for o in outcomes if by_id[o.case_id].latent["segment"] == "dead"]
        ready = [o for o in outcomes if by_id[o.case_id].latent["segment"] == "ready"]
        return {
            "close_lost_rate": sum(1 for o in outcomes if o.effective == CLOSE_LOST) / n,
            "dead_correctly_closed": sum(1 for o in dead if o.effective == CLOSE_LOST) / (len(dead) or 1),
            "ready_abandoned": sum(1 for o in ready if o.effective == CLOSE_LOST) / (len(ready) or 1),
            "call_rate": sum(1 for o in outcomes if o.effective == CALL) / n,
            "mean_touches_used": sum(1 for o in outcomes if o.effective in _CONTACT) / n,
            "request_timeline_rate": sum(1 for o in outcomes if o.effective == REQUEST_TIMELINE) / n,
        }
