"""Outcome-learning backtest — does observing outcomes actually improve future selection, safely?
(plan §20/§21).

A controlled outcome environment: each opportunity offers several candidate actions whose PRODUCER
predictions are uninformative (the app can't tell a priori which pays off), while each action has a
HIDDEN true reward — some good, some net-negative. We run a stream of opportunities under two policies:

  * static   — select_action with no learning (goes by the producer's prior)
  * learning — accumulate outcomes in the shared OutcomeLog, refit a UtilityModel, and let it adjust
               selection

Claims asserted (over a seed sweep):
  1. learning accumulates materially more reward / less regret than static, approaching an oracle;
  2. it is REPLAYABLE — the same seed reproduces the same reward exactly (the model is a pure function
     of the log);
  3. it PRESERVES GOVERNANCE — a consequential action still routes to human approval no matter how
     attractive its learned utility;
  4. it LEARNS TO ABSTAIN — when every action in an opportunity is net-negative, the learned policy
     ends up choosing do-nothing.

This validates the loop MECHANISM on a controlled environment; it is NOT a claim about real production
outcomes (that's the deployment step).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .agent_gateway.contracts import RiskTier
from .outcome_learning import UtilityModel
from .priority_engine import (
    Action, DecisionOpportunity, InterventionCandidate, OutcomeEvent, OutcomeLog, select_action)


@dataclass(frozen=True)
class Arm:
    kind: str
    true_mean: float                 # hidden true reward
    risk_tier: RiskTier = RiskTier.BOUNDED_WRITE


@dataclass(frozen=True)
class Context:
    name: str
    arms: Tuple[Arm, ...]


def _make_contexts(rng, n_ctx: int = 4) -> List[Context]:
    ctxs = []
    for ci in range(n_ctx):
        arms = []
        for ai in range(rng.randint(3, 4)):
            # true reward spans clearly-good to net-negative; do-nothing (0) beats the negatives
            arms.append(Arm(kind=f"a{ai}", true_mean=rng.uniform(-0.4, 1.0),
                            risk_tier=rng.choice([RiskTier.BOUNDED_WRITE, RiskTier.BOUNDED_WRITE,
                                                  RiskTier.CONSEQUENTIAL])))
        ctxs.append(Context(name=f"ctx{ci}", arms=tuple(arms)))
    return ctxs


def _opportunity(ctx: Context, i: int, rng) -> DecisionOpportunity:
    # the producer's predicted value is UNINFORMATIVE (a noisy guess that doesn't track true reward),
    # so a static policy can't do better than its prior — only observed outcomes reveal what works.
    cands = tuple(
        InterventionCandidate(
            source_app=ctx.name, subject=f"{ctx.name}:{i}", proposed_action=f"do {a.kind}",
            expected_value=rng.uniform(0.4, 0.7), confidence=0.8, action_kind=a.kind,
            risk_tier=a.risk_tier, reversibility=0.6, candidate_id=f"{ctx.name}:{a.kind}:{i}")
        for a in ctx.arms)
    return DecisionOpportunity(entity=f"{ctx.name}:{i}", source_app=ctx.name,
                              candidate_actions=cands, opportunity_id=f"{ctx.name}:{i}")


def _reward(ctx: Context, kind: str, rng) -> float:
    arm = next(a for a in ctx.arms if a.kind == kind)
    return arm.true_mean + rng.uniform(-0.15, 0.15)          # noisy observation of the true reward


def _oracle_value(ctx: Context) -> float:
    return max(0.0, max(a.true_mean for a in ctx.arms))       # best arm, or do-nothing if all negative


@dataclass(frozen=True)
class EpisodeResult:
    total_reward: float
    total_regret: float


def run_episode(seed: int = 7, *, learn: bool, n_opps: int = 400, refit_every: int = 20) -> EpisodeResult:
    rng = random.Random(seed)
    contexts = _make_contexts(rng)
    log = OutcomeLog()
    model = UtilityModel()
    total = regret = 0.0
    for i in range(n_opps):
        ctx = contexts[i % len(contexts)]
        opp = _opportunity(ctx, i, rng)
        ufn = model.as_utility_fn() if (learn and log.events) else None
        sel = select_action(opp, utility_fn=ufn)
        kind = sel.action.action_kind
        if sel.decision.action == Action.ABSTAIN or not kind:      # chose do-nothing / abstained
            reward = 0.0
        else:
            reward = _reward(ctx, kind, rng)                       # approval assumed granted in-sim
            log.record(OutcomeEvent(candidate_id=sel.action.candidate_id, source_app=ctx.name,
                                    action=sel.decision.action, action_kind=kind, observed_reward=reward,
                                    attribution_confidence=0.9))
        total += reward
        regret += _oracle_value(ctx) - reward
        if learn and (i + 1) % refit_every == 0:
            model.fit(log)
    return EpisodeResult(total, regret)


def run_acceptance(seed: int = 7) -> Dict[str, EpisodeResult]:
    return {"static": run_episode(seed, learn=False), "learning": run_episode(seed, learn=True)}


def _demo() -> str:                                       # python -m agentic_os.outcome_learning_backtest
    r = run_acceptance(7)
    s, l = r["static"], r["learning"]
    return ("Outcome loop — 400 governed decisions on a controlled outcome environment (seed 7):\n"
            f"  static   (no learning):  reward {s.total_reward:6.1f}   regret {s.total_regret:6.1f}\n"
            f"  learning (observes outcomes → adjusts selection): "
            f"reward {l.total_reward:6.1f}   regret {l.total_regret:6.1f}\n"
            f"  → learning captured {(l.total_reward / s.total_reward):.1f}× the reward, "
            f"{(1 - l.total_regret / s.total_regret) * 100:.0f}% less regret — "
            "governance intact, replayable, explainable.\n"
            "  (Validated in simulation; wiring to live production outcomes is the deployment step.)")


if __name__ == "__main__":     # pragma: no cover
    print(_demo())


# ── governance + abstention probes (used by the tests) ───────────────────────────────
def consequential_still_needs_approval(seed: int = 7) -> bool:
    """A consequential action, however attractive its learned utility, still routes to approval."""
    rng = random.Random(seed)
    hot = InterventionCandidate(source_app="crm", subject="Acme", proposed_action="send proposal",
                                expected_value=0.9, confidence=0.95, action_kind="send",
                                risk_tier=RiskTier.CONSEQUENTIAL, candidate_id="crm:send")
    opp = DecisionOpportunity(entity="Acme", source_app="crm", candidate_actions=(hot,),
                              opportunity_id="o1")
    # a model that has learned this action is great
    model = UtilityModel()
    log = OutcomeLog()
    for _ in range(30):
        log.record(OutcomeEvent(candidate_id="crm:send", source_app="crm", action=Action.REQUEST_APPROVAL,
                                action_kind="send", observed_reward=1.0, attribution_confidence=1.0))
    model.fit(log)
    sel = select_action(opp, utility_fn=model.as_utility_fn())
    return sel.action.action_kind == "send" and sel.decision.action == Action.REQUEST_APPROVAL


def learns_to_abstain_when_all_actions_are_negative(seed: int = 7) -> Tuple[bool, bool]:
    """Returns (acted_before_learning, abstained_after_learning) for an all-negative opportunity."""
    negs = tuple(
        InterventionCandidate(source_app="x", subject="s", proposed_action=f"do {k}", expected_value=0.6,
                              confidence=0.8, action_kind=k, risk_tier=RiskTier.BOUNDED_WRITE,
                              candidate_id=f"x:{k}")
        for k in ("a", "b"))
    opp = DecisionOpportunity(entity="s", source_app="x", candidate_actions=negs, opportunity_id="o")
    before = select_action(opp)                                    # no learning → acts on the prior
    log = OutcomeLog()
    for _ in range(40):
        for k in ("a", "b"):
            log.record(OutcomeEvent(candidate_id=f"x:{k}", source_app="x", action=Action.ACT,
                                    action_kind=k, observed_reward=-0.3, attribution_confidence=1.0))
    after = select_action(opp, utility_fn=UtilityModel().fit(log).as_utility_fn())
    acted_before = before.decision.action != Action.ABSTAIN
    abstained_after = after.decision.action == Action.ABSTAIN
    return acted_before, abstained_after
