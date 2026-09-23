"""Fraud A → Fraud B transfer — same decision family, different business distribution.

This is the complementary transfer axis to Receivables→Stale-Quote. There the decision *structure* was only
partly shared and the action vocabulary differed, so only the abstract *principle* transferred. Here the
family is identical — same observable schema, same bucket function, same actions — so a policy learned on
Fraud A can be applied *directly* to Fraud B. The open question is whether that direct policy survives a
change of business (higher fraud rate, instant delivery, noisier signals, different economics), or whether
the distribution shift breaks it and B must be relearned natively.

Arms (deterministic):
  * ``B_baseline``          — B's naive decline-on-flags rule.
  * ``B_native``            — learn on B (the ceiling for B).
  * ``A_to_B_direct``       — apply A's learned bucket policy directly to B (fall back to A's baseline).
  * ``A_to_B_adapt``        — A's policy as a prior, overridden by B-native experience where B has seen the bucket.
"""
from __future__ import annotations

from typing import Callable, Sequence

from .fraud_world import FraudWorld, fraud_world_a, fraud_world_b
from .harness import (
    ArmReport, DecisionCase, Proposal, _acquire_experience, control_arm, evaluate, learned_arm)


def _policy_arm(world: FraudWorld, policy: dict[str, str],
                fallback: Callable[[DecisionCase], Proposal]) -> Callable[[DecisionCase], Proposal]:
    def arm(c: DecisionCase) -> Proposal:
        b = world.bucket(c.observable)
        if b in policy and world.admissible_action(c.observable, policy[b]):
            return Proposal(policy[b], rationale="transferred Fraud-A policy")
        return fallback(c)
    return arm


def run_fraud_transfer(*, seed: int = 7, n_train: int = 800, n_eval: int = 800) -> dict[str, ArmReport]:
    a, b = fraud_world_a(), fraud_world_b()
    a_train = a.cases(seed=seed, n=n_train)
    b_train = b.cases(seed=seed, n=n_train)
    b_eval = b.cases(seed=seed + 10_000, n=n_eval)

    a_policy = _acquire_experience(a, a_train)          # what worked on Fraud A, per bucket
    b_native = learned_arm(b, b_train)
    b_native_policy = _acquire_experience(b, b_train)

    def adapt(c: DecisionCase) -> Proposal:
        if b.bucket(c.observable) in b_native_policy:
            return b_native(c)
        return _policy_arm(b, a_policy, control_arm(b))(c)

    return {
        "B_baseline": evaluate(b, control_arm(b), b_eval, arm_name="B_baseline"),
        "B_native": evaluate(b, b_native, b_eval, arm_name="B_native"),
        "A_to_B_direct": evaluate(
            b, _policy_arm(b, a_policy, control_arm(b)), b_eval, arm_name="A_to_B_direct"),
        "A_to_B_adapt": evaluate(b, adapt, b_eval, arm_name="A_to_B_adapt"),
    }
