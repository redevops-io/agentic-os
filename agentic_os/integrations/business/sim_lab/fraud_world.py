"""World 4 — Order-fraud review (plan §5, Fraud family).

The decision is NOT "score this order's risk". It is: for this order, do we APPROVE, delay (HOLD), ask the
customer to step up (REQUEST_VERIFICATION), send it to a human (MANUAL_REVIEW), or DECLINE — under
false-positive economics where declining a good customer is a real, costed loss. This is deliberately a
different decision shape from receivables follow-up: the expensive mistake runs in *both* directions
(fraud loss vs false decline), and the robust middle actions (verify / review) exist precisely because
neither approve-all nor decline-all is good.

Ground truth (``latent``) is the order's true ``kind`` (legit / fraud) and amount. An arm never sees that;
it sees only *behavioral* signals (velocity, address/AVS mismatch, account age, order value band, reship).
Those signals are noisy: legit orders sometimes trip them (that is where false declines come from) and some
fraud slips through clean. No protected trait (name, geography, gender, age, ethnicity) is ever an input —
:data:`PROTECTED_TRAITS_NEVER_USED` documents the exclusion and the fraud-safety test enforces it.

The learnable structure: in high-risk observable buckets, REQUEST_VERIFICATION / MANUAL_REVIEW dominate both
APPROVE (which eats chargebacks) and DECLINE (which false-declines the legit fraction), because fraud
abandons a step-up while most legit customers complete it. Verified Experience discovers this per bucket;
the naive baseline (decline on any flag) does not.
"""
from __future__ import annotations

import random
from typing import Any, Mapping, Sequence

from .harness import CaseOutcome, DecisionCase

APPROVE = "approve"
HOLD = "hold"
REQUEST_VERIFICATION = "request_verification"
MANUAL_REVIEW = "manual_review"
DECLINE = "decline"

# The ONLY inputs an arm may read. Behavioral / transactional signals — no identity or protected traits.
BEHAVIORAL_OBSERVABLE_KEYS = frozenset({
    "amount_band", "velocity_flag", "mismatch_flag", "new_account_flag", "high_value_flag", "reship_flag"})
PROTECTED_TRAITS_NEVER_USED = frozenset({
    "name", "gender", "age", "ethnicity", "country", "zip", "email_domain", "ip_geo"})

# Economics, in integer cents. Margin is what a fulfilled legit sale earns; a fulfilled fraud order loses the
# goods + amount + a chargeback fee; a false decline loses the margin plus goodwill; verify/review cost real
# money and shed a fraction of legit customers to friction.
_MARGIN_RATE = 0.35
_CHARGEBACK_FEE = 2_500
_REVIEW_COST = 900
_VERIFY_COST = 200
_GOODWILL = 1_500
_STEP_UP_ABANDON_LEGIT = 0.10          # legit customers who give up at a step-up
_HOLD_ABANDON_LEGIT = 0.15             # legit customers lost to a delay
_HOLD_COMPLETE_FRAUD = 0.20            # fraud that still completes despite a hold


class FraudWorld:
    world_id = "fraud_order_review/v1"

    def actions(self) -> tuple[str, ...]:
        return (APPROVE, HOLD, REQUEST_VERIFICATION, MANUAL_REVIEW, DECLINE)

    def hold_actions(self) -> frozenset[str]:
        # "no active, customer-facing intervention beyond letting it sit" — HOLD only. verify/review/decline
        # are all real interventions with cost, and DECLINE is the strongest one.
        return frozenset({HOLD})

    def default_hold(self) -> str:
        return HOLD

    # ---- oracle (reads latent only) -------------------------------------------------
    def net_value(self, latent: Mapping[str, Any], action: str) -> float:
        amount = latent["amount_cents"]
        margin = amount * _MARGIN_RATE
        if latent["kind"] == "legit":
            if action == APPROVE:
                return margin
            if action == REQUEST_VERIFICATION:
                return margin * (1 - _STEP_UP_ABANDON_LEGIT) - _VERIFY_COST
            if action == MANUAL_REVIEW:
                return margin - _REVIEW_COST
            if action == HOLD:
                return margin * (1 - _HOLD_ABANDON_LEGIT)
            if action == DECLINE:
                return -( margin + _GOODWILL )              # lost sale + goodwill damage
        else:  # fraud
            if action == APPROVE:
                return -(amount + _CHARGEBACK_FEE)          # goods gone + chargeback
            if action == REQUEST_VERIFICATION:
                return -_VERIFY_COST                         # fraud abandons the step-up; loss avoided
            if action == MANUAL_REVIEW:
                return -_REVIEW_COST                         # caught by a human; loss avoided, analyst paid
            if action == HOLD:
                return -_HOLD_COMPLETE_FRAUD * (amount + _CHARGEBACK_FEE)
            if action == DECLINE:
                return 0.0                                   # avoided cleanly
        raise ValueError(action)

    def optimal(self, latent: Mapping[str, Any]) -> str:
        return max(self.actions(), key=lambda a: self.net_value(latent, a))

    # ---- feasibility (reads observable only) ----------------------------------------
    def admissible_action(self, observable: Mapping[str, Any], action: str) -> bool:
        # manual review is a scarce human resource: only feasible on genuinely ambiguous / high-value orders,
        # not on every order (a benchmark that let every order go to a human would be unrealistic).
        if action == MANUAL_REVIEW:
            return bool(observable.get("high_value_flag") or observable.get("mismatch_flag"))
        return True

    # ---- learning key ---------------------------------------------------------------
    def bucket(self, observable: Mapping[str, Any]) -> str:
        flags = "".join("1" if observable.get(k) else "0" for k in (
            "velocity_flag", "mismatch_flag", "new_account_flag", "high_value_flag", "reship_flag"))
        return f"{observable.get('amount_band','?')}:{flags}"

    # ---- arm A baseline: the common naive rule — decline on any risk flag ------------
    def baseline_action(self, observable: Mapping[str, Any]) -> str:
        risk = sum(1 for k in ("velocity_flag", "mismatch_flag", "new_account_flag", "reship_flag")
                   if observable.get(k))
        if risk >= 2:
            return DECLINE
        if risk == 1:
            return REQUEST_VERIFICATION
        return APPROVE

    # ---- corpus (§5 reset/observe) --------------------------------------------------
    def cases(self, *, seed: int, n: int, shift: float = 0.0) -> tuple[DecisionCase, ...]:
        rng = random.Random(seed)
        base_fraud_rate = min(0.6, max(0.02, 0.18 + shift))   # covariate/base-rate shift on eval only
        out: list[DecisionCase] = []
        for i in range(n):
            is_fraud = rng.random() < base_fraud_rate
            amount = rng.choice([3_500, 8_000, 15_000, 40_000, 90_000])
            high_value = amount >= 40_000
            # signal model: fraud lights up flags more often, but neither side is separable.
            def flag(p_fraud: float, p_legit: float) -> bool:
                return rng.random() < (p_fraud if is_fraud else p_legit)
            velocity = flag(0.55, 0.10)
            mismatch = flag(0.50, 0.12)
            new_acct = flag(0.60, 0.25)
            reship = flag(0.35, 0.05)
            observable = {
                "amount_band": ("xl" if amount >= 90_000 else "l" if high_value else
                                "m" if amount >= 8_000 else "s"),
                "velocity_flag": velocity, "mismatch_flag": mismatch, "new_account_flag": new_acct,
                "high_value_flag": high_value, "reship_flag": reship}
            latent = {"kind": "fraud" if is_fraud else "legit", "amount_cents": amount}
            out.append(DecisionCase(
                case_id=f"{self.world_id}:{seed}:{i}", world_id=self.world_id,
                observable=observable, latent=latent, known_at=i))
        return tuple(out)

    # ---- operational metrics the reframe wants surfaced -----------------------------
    def extra_metrics(self, outcomes: Sequence[CaseOutcome],
                      cases: Sequence[DecisionCase]) -> Mapping[str, float]:
        by_id = {c.case_id: c for c in cases}
        legit = [o for o in outcomes if by_id[o.case_id].latent["kind"] == "legit"]
        fraud = [o for o in outcomes if by_id[o.case_id].latent["kind"] == "fraud"]
        n = len(outcomes) or 1
        false_declines = sum(1 for o in legit if o.effective == DECLINE)
        fraud_approved = sum(1 for o in fraud if o.effective == APPROVE)
        return {
            "false_decline_rate": false_declines / (len(legit) or 1),   # good customers we turned away
            "fraud_loss_rate": fraud_approved / (len(fraud) or 1),       # fraud we let through
            "manual_review_rate": sum(1 for o in outcomes if o.effective == MANUAL_REVIEW) / n,
            "verification_rate": sum(1 for o in outcomes if o.effective == REQUEST_VERIFICATION) / n,
            "approval_rate": sum(1 for o in outcomes if o.effective == APPROVE) / n,
        }
