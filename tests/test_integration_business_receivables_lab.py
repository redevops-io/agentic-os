"""Phase E — the Receivables decision-learning experiment.

SYNTHETIC and honestly labeled: outcomes come from a hidden generative model, not real receivables, and
the arms are policy strategies, not a live LLM. What is established offline: (a) "do nothing" is
decision-relevant, so a naive intervene-everything agent pays real regret; (b) a bounded, verified-
Experience learner reduces that regret; (c) it does so WITHOUT intervening more. The real result (same
frozen model, real data, lower collections regret) is the next gate — decision-bench territory.
"""
from __future__ import annotations

from collections import Counter

import pytest

from agentic_os.integrations.business import (
    InterventionKind, LearnBoundaryError, assert_strategy_only, make_corpus, net_value, optimal_action,
    run_experiment)
from agentic_os.integrations.business.receivables_lab import LatentAccount


# ── the environment is principled: do-nothing / route-to-human genuinely matters ─────
def test_optimal_actions_make_hold_and_human_review_common():
    mix = Counter(optimal_action(l).value for l in make_corpus())
    hold_like = mix[InterventionKind.HOLD.value] + mix[InterventionKind.HUMAN_REVIEW.value]
    # a large fraction of cases are best handled by NOT sending a reminder — the crux of the reframe
    assert hold_like / sum(mix.values()) > 0.4


def test_outcome_oracle_sanity_per_latent_kind():
    pays = LatentAccount("p", 300000, 20, "pays_soon", 0.8)
    nudge = LatentAccount("n", 300000, 50, "needs_nudge", 0.3)
    disputed = LatentAccount("d", 300000, 40, "disputed", 0.5)
    assert optimal_action(pays) is InterventionKind.HOLD              # pays anyway → don't spend/damage
    assert optimal_action(nudge) in (InterventionKind.SOFT_REMINDER, InterventionKind.DIRECT_REMINDER,
                                     InterventionKind.PAYMENT_PLAN, InterventionKind.ESCALATION)
    assert optimal_action(disputed) is InterventionKind.HUMAN_REVIEW
    # nudging a pays-soon account is strictly worse than holding it (wasted cost + relationship damage)
    assert net_value(pays, InterventionKind.DIRECT_REMINDER) < net_value(pays, InterventionKind.HOLD)


# ── the thesis: experience reduces regret, and NOT by over-intervening ───────────────
def test_learned_arm_beats_naive_and_holds_the_line_on_over_intervention():
    rep = run_experiment()
    A, B, G = rep["A_control"], rep["B_naive"], rep["G_learned"]

    # 1. the learner has substantially lower regret than the naive frozen agent
    assert G.mean_regret_cents < 0.25 * B.mean_regret_cents
    # 2. it also beats the deterministic control arm
    assert G.mean_regret_cents < A.mean_regret_cents
    # 3. crucially, it does NOT win by intervening more: the naive agent over-intervenes heavily,
    #    the learner barely at all (guards the reframe's failure mode)
    assert B.unnecessary_intervention_rate > 0.4
    assert G.unnecessary_intervention_rate <= A.unnecessary_intervention_rate <= 0.05
    # 4. higher net value overall
    assert G.mean_net_value_cents > B.mean_net_value_cents


def test_experiment_is_deterministic_and_replayable():
    a = run_experiment(seed=7)
    b = run_experiment(seed=7)
    assert a["G_learned"].mean_regret_cents == b["G_learned"].mean_regret_cents
    assert a["B_naive"].unnecessary_intervention_rate == b["B_naive"].unnecessary_intervention_rate


# ── the Learn boundary is enforced (strategy-only) ───────────────────────────────────
def test_learn_boundary_is_strategy_only():
    assert_strategy_only("intervention_selection")       # allowed strategy dimension
    for forbidden in ("authorization", "approval_requirement", "financial_limit", "verification_requirement"):
        with pytest.raises(LearnBoundaryError):
            assert_strategy_only(forbidden)
    with pytest.raises(LearnBoundaryError):
        assert_strategy_only("something_unknown")        # unknown → refused too (fail-closed)
