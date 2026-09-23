"""World 4 — Order-fraud review (plan §5, Fraud family), now profile-driven for A→B transfer.

The decision is NOT "score this order's risk". It is: for this order, do we APPROVE, delay (HOLD), ask the
customer to step up (REQUEST_VERIFICATION), send it to a human (MANUAL_REVIEW), or DECLINE — under
false-positive economics where declining a good customer is a real, costed loss. The expensive mistake runs
in *both* directions (fraud loss vs false decline), which is what makes verify / review the robust middle.

Ground truth (``latent``) is the order's true ``kind`` (legit / fraud) and amount. An arm sees only
*behavioral* signals (velocity, address/AVS mismatch, account age, order value band, reship). No protected
trait is ever an input (:data:`PROTECTED_TRAITS_NEVER_USED`, enforced by test).

A :class:`FraudProfile` captures one *business*: its economics, fraud base rate, order sizes, and — crucially
— how separable its signals are. Two profiles ship:

  * ``FRAUD_A`` — card-not-present e-commerce (physical goods): moderate fraud rate, fairly discriminative
    signals. (Identical numerics to the original world, so its golden is unchanged.)
  * ``FRAUD_B`` — digital-goods marketplace: higher fraud rate, instant delivery (a hold rarely stops fraud),
    thinner margins-of-error and **noisier signals** (sophisticated fraud looks more like legit) → a higher
    irreducible evidence floor.

Same decision family, different data-generating distribution: exactly the ``Fraud A → Fraud B`` transfer axis
(same structure, different business) that complements Receivables→Stale-Quote (different task, shared
structure). See ``fraud_transfer``.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, Tuple

from .harness import CaseOutcome, DecisionCase

APPROVE = "approve"
HOLD = "hold"
REQUEST_VERIFICATION = "request_verification"
MANUAL_REVIEW = "manual_review"
DECLINE = "decline"

BEHAVIORAL_OBSERVABLE_KEYS = frozenset({
    "amount_band", "velocity_flag", "mismatch_flag", "new_account_flag", "high_value_flag", "reship_flag"})
PROTECTED_TRAITS_NEVER_USED = frozenset({
    "name", "gender", "age", "ethnicity", "country", "zip", "email_domain", "ip_geo"})

_FLAG_KEYS = ("velocity_flag", "mismatch_flag", "new_account_flag", "reship_flag")


@dataclass(frozen=True)
class FraudProfile:
    """One business's fraud environment. All money in integer cents."""
    world_id: str
    margin_rate: float
    chargeback_fee: int
    review_cost: int
    verify_cost: int
    goodwill: int
    step_up_abandon_legit: float     # legit customers who give up at a step-up
    hold_abandon_legit: float        # legit customers lost to a delay
    hold_complete_fraud: float       # fraud that still completes despite a hold
    base_fraud_rate: float
    amounts: Tuple[int, ...]
    mid_threshold: int               # amount_band m boundary
    high_value_threshold: int        # high_value flag / band l boundary
    xl_threshold: int
    signal_probs: Tuple[Tuple[str, float, float], ...]  # (flag_key, p_if_fraud, p_if_legit), fixed order


FRAUD_A = FraudProfile(
    world_id="fraud_order_review/v1", margin_rate=0.35, chargeback_fee=2_500, review_cost=900,
    verify_cost=200, goodwill=1_500, step_up_abandon_legit=0.10, hold_abandon_legit=0.15,
    hold_complete_fraud=0.20, base_fraud_rate=0.18,
    amounts=(3_500, 8_000, 15_000, 40_000, 90_000), mid_threshold=8_000, high_value_threshold=40_000,
    xl_threshold=90_000,
    signal_probs=(("velocity_flag", 0.55, 0.10), ("mismatch_flag", 0.50, 0.12),
                  ("new_account_flag", 0.60, 0.25), ("reship_flag", 0.35, 0.05)))

# Digital-goods marketplace: more fraud, instant delivery (hold barely helps), thinner margins for error and
# markedly noisier signals — fraud and legit overlap far more, raising the irreducible evidence floor.
FRAUD_B = FraudProfile(
    world_id="fraud_order_review/marketplace-digital/v1", margin_rate=0.70, chargeback_fee=2_500,
    review_cost=600, verify_cost=150, goodwill=800, step_up_abandon_legit=0.15, hold_abandon_legit=0.25,
    hold_complete_fraud=0.35, base_fraud_rate=0.30,
    amounts=(1_500, 4_000, 9_000, 20_000, 45_000), mid_threshold=4_000, high_value_threshold=20_000,
    xl_threshold=45_000,
    signal_probs=(("velocity_flag", 0.45, 0.22), ("mismatch_flag", 0.38, 0.20),
                  ("new_account_flag", 0.52, 0.38), ("reship_flag", 0.20, 0.10)))


class FraudWorld:
    def __init__(self, profile: FraudProfile = FRAUD_A):
        self.profile = profile
        self.world_id = profile.world_id

    def actions(self) -> tuple[str, ...]:
        return (APPROVE, HOLD, REQUEST_VERIFICATION, MANUAL_REVIEW, DECLINE)

    def hold_actions(self) -> frozenset[str]:
        return frozenset({HOLD})     # verify/review/decline are all real, costed interventions

    def default_hold(self) -> str:
        return HOLD

    # ---- oracle (reads latent only) -------------------------------------------------
    def net_value(self, latent: Mapping[str, Any], action: str) -> float:
        p = self.profile
        amount = latent["amount_cents"]
        margin = amount * p.margin_rate
        if latent["kind"] == "legit":
            if action == APPROVE:
                return margin
            if action == REQUEST_VERIFICATION:
                return margin * (1 - p.step_up_abandon_legit) - p.verify_cost
            if action == MANUAL_REVIEW:
                return margin - p.review_cost
            if action == HOLD:
                return margin * (1 - p.hold_abandon_legit)
            if action == DECLINE:
                return -(margin + p.goodwill)
        else:  # fraud
            if action == APPROVE:
                return -(amount + p.chargeback_fee)
            if action == REQUEST_VERIFICATION:
                return -p.verify_cost
            if action == MANUAL_REVIEW:
                return -p.review_cost
            if action == HOLD:
                return -p.hold_complete_fraud * (amount + p.chargeback_fee)
            if action == DECLINE:
                return 0.0
        raise ValueError(action)

    def optimal(self, latent: Mapping[str, Any]) -> str:
        return max(self.actions(), key=lambda a: self.net_value(latent, a))

    # ---- feasibility (reads observable only) ----------------------------------------
    def admissible_action(self, observable: Mapping[str, Any], action: str) -> bool:
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
        risk = sum(1 for k in _FLAG_KEYS if observable.get(k))
        if risk >= 2:
            return DECLINE
        if risk == 1:
            return REQUEST_VERIFICATION
        return APPROVE

    # ---- corpus ---------------------------------------------------------------------
    def cases(self, *, seed: int, n: int, shift: float = 0.0) -> tuple[DecisionCase, ...]:
        p = self.profile
        rng = random.Random(seed)
        base_fraud_rate = min(0.7, max(0.02, p.base_fraud_rate + shift))
        out: list[DecisionCase] = []
        for i in range(n):
            is_fraud = rng.random() < base_fraud_rate
            amount = rng.choice(p.amounts)
            high_value = amount >= p.high_value_threshold
            flags = {}
            for key, p_fraud, p_legit in p.signal_probs:      # fixed order → deterministic
                flags[key] = rng.random() < (p_fraud if is_fraud else p_legit)
            observable = {
                "amount_band": ("xl" if amount >= p.xl_threshold else "l" if high_value else
                                "m" if amount >= p.mid_threshold else "s"),
                "high_value_flag": high_value, **flags}
            latent = {"kind": "fraud" if is_fraud else "legit", "amount_cents": amount}
            out.append(DecisionCase(
                case_id=f"{self.world_id}:{seed}:{i}", world_id=self.world_id,
                observable=observable, latent=latent, known_at=i))
        return tuple(out)

    # ---- operational metrics --------------------------------------------------------
    def extra_metrics(self, outcomes: Sequence[CaseOutcome],
                      cases: Sequence[DecisionCase]) -> Mapping[str, float]:
        by_id = {c.case_id: c for c in cases}
        legit = [o for o in outcomes if by_id[o.case_id].latent["kind"] == "legit"]
        fraud = [o for o in outcomes if by_id[o.case_id].latent["kind"] == "fraud"]
        n = len(outcomes) or 1
        return {
            "false_decline_rate": sum(1 for o in legit if o.effective == DECLINE) / (len(legit) or 1),
            "fraud_loss_rate": sum(1 for o in fraud if o.effective == APPROVE) / (len(fraud) or 1),
            "manual_review_rate": sum(1 for o in outcomes if o.effective == MANUAL_REVIEW) / n,
            "verification_rate": sum(1 for o in outcomes if o.effective == REQUEST_VERIFICATION) / n,
            "approval_rate": sum(1 for o in outcomes if o.effective == APPROVE) / n,
        }


def fraud_world_a() -> FraudWorld:
    return FraudWorld(FRAUD_A)


def fraud_world_b() -> FraudWorld:
    return FraudWorld(FRAUD_B)
