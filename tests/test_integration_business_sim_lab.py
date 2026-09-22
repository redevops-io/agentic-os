"""Acceptance tests for the Business Simulation Lab (plan §29 gates), Fraud world.

Covers, offline and deterministic: world validity, the §7 mutation suite (defects the harness must catch),
fraud-safety (§21: no protected traits, decline-everything must fail), regret discrimination, that verified
Experience beats the naive baseline, and transfer isolation under covariate shift. The live frozen-model
gate is a separate, opt-in test that skips when the endpoint is down.
"""
from __future__ import annotations

import pytest

from agentic_os.integrations.business.receipts import VerificationState
from agentic_os.integrations.business.transitions import Admissibility
from agentic_os.integrations.business.sim_lab import (
    BEHAVIORAL_OBSERVABLE_KEYS, FraudWorld, PROTECTED_TRAITS_NEVER_USED, apply_kernel, control_arm,
    evaluate, learned_arm, run_world)
from agentic_os.integrations.business.sim_lab.fraud_world import (
    APPROVE, DECLINE, HOLD, MANUAL_REVIEW, REQUEST_VERIFICATION)


# --------------------------------------------------------------------------- world validity (§29 Gate 1)
def test_world_cases_are_well_formed_and_hide_ground_truth():
    world = FraudWorld()
    cases = world.cases(seed=1, n=200)
    assert len(cases) == 200
    for c in cases:
        # an arm sees ONLY behavioral signals; latent ground truth never leaks into observable
        assert set(c.observable) <= BEHAVIORAL_OBSERVABLE_KEYS
        assert "kind" not in c.observable and "amount_cents" not in c.observable
        assert c.latent["kind"] in ("legit", "fraud")
        # the per-case optimal is a real action with the best oracle value
        opt = world.optimal(c.latent)
        assert opt in world.actions()
        assert all(world.net_value(c.latent, opt) >= world.net_value(c.latent, a) for a in world.actions())


def test_optimal_action_matches_the_family_economics():
    world = FraudWorld()
    assert world.optimal({"kind": "legit", "amount_cents": 15_000}) == APPROVE
    assert world.optimal({"kind": "fraud", "amount_cents": 90_000}) == DECLINE


# --------------------------------------------------------------------------- fraud safety (§21)
def test_no_protected_trait_is_ever_an_input():
    assert BEHAVIORAL_OBSERVABLE_KEYS.isdisjoint(PROTECTED_TRAITS_NEVER_USED)
    world = FraudWorld()
    for c in world.cases(seed=2, n=100):
        assert set(c.observable).isdisjoint(PROTECTED_TRAITS_NEVER_USED)


def test_decline_everything_fails():
    """A blanket-decline policy avoids all fraud but must score badly through false declines — otherwise the
    benchmark isn't measuring the real trade-off (§21)."""
    world = FraudWorld()
    cases = world.cases(seed=5, n=400)
    from agentic_os.integrations.business.sim_lab.harness import Proposal
    rep = evaluate(world, lambda c: Proposal(DECLINE), cases, arm_name="decline_all")
    assert rep.extra["false_decline_rate"] > 0.5          # turns away most good customers
    assert rep.extra["fraud_loss_rate"] == 0.0            # (it does catch all fraud)
    assert rep.mean_regret > 0                            # and is punished for it


# --------------------------------------------------------------------------- §7 mutation suite
def test_kernel_rejected_action_never_executes():
    # a rejected (unapproved) action must not count as executed → cannot evolve state or become Experience
    r = apply_kernel(state_valid=True, action_admissible=True, approved=False,
                     provider_ok=True, reconciled=True)
    assert r.admissibility is Admissibility.REJECTED and not r.executed


def test_kernel_inadmissible_action_never_executes():
    r = apply_kernel(state_valid=True, action_admissible=False, approved=True,
                     provider_ok=True, reconciled=True)
    assert r.admissibility is Admissibility.INADMISSIBLE_ACTION and not r.executed


def test_kernel_provider_success_is_not_verification():
    # mutation: counting a provider "ok" as a verified outcome. Without reconciliation it must stay UNKNOWN.
    r = apply_kernel(state_valid=True, action_admissible=True, approved=True,
                     provider_ok=True, reconciled=None)
    assert r.verification_state is VerificationState.UNKNOWN and r.outcome_pending
    r2 = apply_kernel(state_valid=True, action_admissible=True, approved=True,
                      provider_ok=False, reconciled=None)
    assert not r2.executed and r2.verification_state is VerificationState.REFUTED


def test_hold_is_not_a_free_relabel_of_an_intervention():
    # mutation: pretending HOLD earns the same as acting. HOLD must have its own (different) oracle value.
    world = FraudWorld()
    legit = {"kind": "legit", "amount_cents": 15_000}
    assert world.net_value(legit, HOLD) != world.net_value(legit, APPROVE)
    fraud = {"kind": "fraud", "amount_cents": 40_000}
    assert world.net_value(fraud, HOLD) != world.net_value(fraud, DECLINE)


def test_manual_review_capacity_is_enforced():
    world = FraudWorld()
    assert not world.admissible_action(
        {"high_value_flag": False, "mismatch_flag": False}, MANUAL_REVIEW)
    assert world.admissible_action({"high_value_flag": True}, MANUAL_REVIEW)


# --------------------------------------------------------------------------- learning result (§29 Gate 2/4)
def test_verified_experience_beats_naive_baseline_on_heldout():
    world = FraudWorld()
    train = world.cases(seed=7, n=600)
    eval_cases = world.cases(seed=17_000, n=400)          # disjoint seed → held-out
    control = evaluate(world, control_arm(world), eval_cases, arm_name="A")
    learned = evaluate(world, learned_arm(world, train), eval_cases, arm_name="GH")
    assert learned.mean_regret < control.mean_regret
    # and it does so without just declining more — over-intervention must not blow up
    assert learned.extra["false_decline_rate"] <= control.extra["false_decline_rate"] + 0.05


def test_learned_policy_survives_covariate_shift():
    """Transfer isolation (§29 Gate 5): a policy learned on one fraud base-rate must still beat the baseline
    when the eval base-rate shifts — otherwise it memorised the training mix."""
    world = FraudWorld()
    train = world.cases(seed=7, n=600)
    shifted = world.cases(seed=17_000, n=400, shift=0.20)  # much higher fraud rate at eval time
    control = evaluate(world, control_arm(world), shifted, arm_name="A")
    learned = evaluate(world, learned_arm(world, train), shifted, arm_name="GH")
    assert learned.mean_regret < control.mean_regret


def test_deterministic_manifest_matches_frozen_golden():
    """§33 — the deterministic run is a stable, reproducible artifact. Regenerate with UPDATE_GOLDEN=1."""
    import json
    import os
    import pathlib
    got = run_world(FraudWorld(), seed=7, n_train=600, n_eval=400).as_dict()
    path = pathlib.Path(__file__).parent / "golden" / "sim_lab_fraud.json"
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n")
    assert got == json.loads(path.read_text())


def test_run_world_manifest_is_serializable_and_complete():
    manifest = run_world(FraudWorld(), seed=7, n_train=400, n_eval=200)
    d = manifest.as_dict()
    assert d["benchmark_version"] == "business-sim-lab/v1"
    assert d["world_id"] == "fraud_order_review/v1"
    assert set(d["arms"]) == {"A_control", "GH_learned"}
    assert d["arms"]["GH_learned"]["mean_regret"] < d["arms"]["A_control"]["mean_regret"]
    assert "false_decline_rate" in d["arms"]["GH_learned"]["extra"]


# --------------------------------------------------------------------------- live frozen-model gate (opt-in)
def test_frozen_model_action_parse_is_robust():
    from agentic_os.integrations.business.sim_lab.model_arm import _parse
    assert _parse("approve") == APPROVE
    assert _parse("I'd request_verification here.") == REQUEST_VERIFICATION
    assert _parse("send to manual_review") == MANUAL_REVIEW
    assert _parse("no clue") == REQUEST_VERIFICATION      # unparseable → conservative middle


@pytest.mark.skipif(
    not __import__("agentic_os.integrations.business.sim_lab", fromlist=["endpoint_reachable"]
                   ).endpoint_reachable(), reason="frozen-model endpoint not reachable")
def test_same_frozen_model_with_experience_is_not_harmful_on_fraud():
    from agentic_os.integrations.business.sim_lab import run_model_experiment
    rep = run_model_experiment(n=6)
    ne, we = rep["model_no_experience"], rep["model_with_experience"]
    for r in (ne, we):
        assert r.n == 6
    # verified Experience must not make the SAME model dramatically worse; the strong improvement is
    # captured/reported, not asserted tight against LLM non-determinism.
    assert we.mean_regret <= ne.mean_regret * 2 + 1
