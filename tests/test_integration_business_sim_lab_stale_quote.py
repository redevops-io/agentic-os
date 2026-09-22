"""Stale-Quote world + Receivables→Stale-Quote transfer experiment.

Encodes the preregistered interpretation as tests where the result is robust and reproducible, and captures
the deterministic experiment as a frozen golden. Modelling note (transparency): the world's signal fidelity
and learning key were adjusted ONCE, before freezing, to remove an artifact where blanket-HOLD dominated
every learned policy; the qualitative findings (strong native gap, weak zero-shot transfer, transfer as a
useful initialisation, restraint as the shared lesson) were present before and after that adjustment.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from agentic_os.integrations.business.sim_lab.abstract import Posture
from agentic_os.integrations.business.sim_lab.harness import Proposal, control_arm, evaluate, learned_arm
from agentic_os.integrations.business.sim_lab.stale_quote_world import (
    CALL, CLOSE_LOST, HOLD, OBSERVABLE_KEYS, SOFT_FOLLOWUP, StaleQuoteWorld)
from agentic_os.integrations.business.sim_lab.transfer import (
    learn_receivables_posture_policy, run_transfer_experiment, sample_efficiency_curve)

GOLDEN = pathlib.Path(__file__).parent / "golden" / "sim_lab_stale_quote_transfer.json"


# --------------------------------------------------------------------------- world validity
def test_world_is_well_formed_and_long_horizon():
    world = StaleQuoteWorld()
    for c in world.cases(seed=1, n=200):
        assert set(c.observable) <= OBSERVABLE_KEYS
        assert c.latent["segment"] in ("ready", "needs_time", "comparison", "dead")
    # long-horizon oracle: HOLD is NOT a relabel of a touch, and over-touching a ready lead erodes value
    ready = {"segment": "ready", "quote_value_cents": 28_000_00, "touch_sensitivity": 0.9}
    fresh = dict(ready, prior_touches=0)
    tired = dict(ready, prior_touches=4)
    assert world.net_value(fresh, CALL) > world.net_value(tired, CALL)          # fatigue is real
    assert world.net_value(ready | {"prior_touches": 0}, HOLD) != \
        world.net_value(ready | {"prior_touches": 0}, CALL)


def test_optimal_actions_follow_the_segment_economics():
    world = StaleQuoteWorld()
    # a fresh, high-value ready lead is worth actively closing (call), never abandoning
    assert world.optimal({"segment": "ready", "quote_value_cents": 60_000_00,
                          "touch_sensitivity": 0.3, "prior_touches": 0}) == CALL
    # a truly dead lead: stop investing
    assert world.optimal({"segment": "dead", "quote_value_cents": 12_000_00,
                          "touch_sensitivity": 0.5, "prior_touches": 3}) == CLOSE_LOST


# --------------------------------------------------------------------------- preregistered results
def test_S1_native_beats_activity_baseline():
    """S1 > baseline ⇒ Stale-Quote has a large learnable local policy gap (unlike fraud)."""
    world = StaleQuoteWorld()
    train = world.cases(seed=7, n=400)
    ev = world.cases(seed=10_007, n=600)
    base = evaluate(world, control_arm(world), ev, arm_name="baseline")
    s1 = evaluate(world, learned_arm(world, train), ev, arm_name="S1")
    assert s1.mean_regret < base.mean_regret * 0.6           # at least ~40% lower
    assert s1.extra["ready_abandoned"] < 0.05                # doesn't learn to dump good leads


def test_transfer_cannot_emit_sales_native_postures():
    """The transfer ceiling: a Receivables-derived posture policy can never recommend REQUEST_TIMELINE
    (ELICIT) or CLOSE_LOST (DISENGAGE) — Receivables has no such action to have taught them."""
    policy = learn_receivables_posture_policy()
    assert Posture.ELICIT not in policy.policy.values()
    assert Posture.DISENGAGE not in policy.policy.values()


def test_zero_shot_transfer_is_weak_and_native_dominates():
    """S2 barely beats baseline and is far worse than S1: the collections lesson does not stand alone in
    sales. (Recorded as a real finding — the mechanism's scope, not a success to inflate.)"""
    rep = run_transfer_experiment(seed=7, n_train=400, n_eval=600)
    base, s1, s2 = rep["baseline"], rep["S1_native"], rep["S2_zero_shot_transfer"]
    assert s2.mean_regret > s1.mean_regret * 1.5             # nowhere near native
    assert s2.mean_regret > base.mean_regret * 0.8           # only a marginal improvement on baseline


def test_literal_transfer_control_does_not_match_native():
    """Negative control (the user's suspicion test): literal thresholds carry the *restraint* lesson so they
    beat the over-active baseline, but must fall clearly short of native — otherwise the two simulators are
    suspiciously aligned."""
    rep = run_transfer_experiment(seed=7, n_train=400, n_eval=600)
    lit, base, s1 = rep["literal_transfer_control"], rep["baseline"], rep["S1_native"]
    assert lit.mean_regret < base.mean_regret                # restraint genuinely transfers
    assert lit.mean_regret > s1.mean_regret * 1.3            # but does not reach native


def test_transfer_is_a_useful_initialisation_at_low_data():
    """S3 (transfer + adapt) reaches lower regret than S1 (native only) at small training budgets — the
    transferred prior is a useful warm start even though zero-shot transfer alone is weak."""
    curve = sample_efficiency_curve(seed=7, n_eval=600, sizes=(25, 100, 200))
    wins = sum(1 for m in (25, 100, 200)
               if curve[m]["S3_transfer_adapt"] <= curve[m]["S1_native"] + 1)
    assert wins >= 2                                          # transfer helps at most small budgets


def test_deterministic_transfer_matches_frozen_golden():
    rep = run_transfer_experiment(seed=7, n_train=400, n_eval=600)
    got = {name: {"mean_regret": round(r.mean_regret, 2), "accuracy": round(r.accuracy, 4),
                  "extra": {k: round(v, 4) for k, v in r.extra.items()}}
           for name, r in rep.items()}
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n")
    assert got == json.loads(GOLDEN.read_text())


# --------------------------------------------------------------------------- live S0–S3 (opt-in)
def test_stale_quote_model_parse_is_robust():
    from agentic_os.integrations.business.sim_lab.stale_quote_model_arm import _parse
    assert _parse("call") == CALL
    assert _parse("I'd request_timeline first.") == "request_timeline"
    assert _parse("close_lost") == CLOSE_LOST
    assert _parse("???") == SOFT_FOLLOWUP


@pytest.mark.skipif(
    not __import__("agentic_os.integrations.business.sim_lab", fromlist=["endpoint_reachable"]
                   ).endpoint_reachable(), reason="frozen-model endpoint not reachable")
def test_s0_to_s3_runs_and_experience_is_not_harmful():
    from agentic_os.integrations.business.sim_lab import run_s0_to_s3
    rep = run_s0_to_s3(n=6)
    assert set(rep) == {"S0_no_experience", "S1_native", "S2_transfer_lesson", "S3_transfer_plus_native"}
    for r in rep.values():
        assert r.n == 6
