"""Supplier Invoice Control (reconciliation axis) + the prospective-H machinery.

Deterministic and reproducible. The live prospective H test (frozen model S0→S1) is exploratory and recorded
in BENCHMARK_PROVENANCE.md; here we assert the world's structure (a real evidence floor that REQUEST_EVIDENCE
serves, generic suspicion and blind approval both lose, a learnable gap) and that the prospective machinery
fixes its prediction before the reveal.
"""
from __future__ import annotations

import json
import os
import pathlib

from agentic_os.integrations.business.sim_lab import (
    SUPPLIER_OBSERVABLE_KEYS, SupplierInvoiceWorld, evidence_floor_regret, predict_learn_opportunity,
    profile_world, reveal_learn_outcome)
from agentic_os.integrations.business.sim_lab.harness import Proposal, control_arm, evaluate, learned_arm
from agentic_os.integrations.business.sim_lab.misalignment import (
    CAPTURE_THRESHOLD, bucket_oracle_policy)
from agentic_os.integrations.business.sim_lab.supplier_invoice_world import (
    APPROVE, REQUEST_EVIDENCE, SupplierInvoiceWorld as W)

GOLDEN = pathlib.Path(__file__).parent / "golden" / "sim_lab_supplier_invoice.json"


def test_world_validity_hides_latent():
    world = SupplierInvoiceWorld()
    for c in world.cases(seed=1, n=300):
        assert set(c.observable) <= SUPPLIER_OBSERVABLE_KEYS
        assert "kind" not in c.observable and "discrepancy_cents" not in c.observable
        assert c.latent["kind"] in ("clean", "legit_variance", "overcharge", "duplicate", "short_delivery")


def test_evidence_acquisition_is_part_of_the_optimal_observable_policy():
    """REQUEST_EVIDENCE is never the per-latent optimum, but the best OBSERVABLE policy uses it for ambiguous
    material buckets — the reconciliation-specific structure and the source of the evidence floor."""
    world = SupplierInvoiceWorld()
    train = world.cases(seed=7, n=2000)
    policy = bucket_oracle_policy(world, train)
    assert REQUEST_EVIDENCE in policy.values()                    # buying evidence is genuinely optimal somewhere
    # ...yet never per-latent optimal (knowing the truth, you dispose directly)
    ev = world.cases(seed=99, n=800)
    assert all(world.optimal(c.latent) != REQUEST_EVIDENCE for c in ev)


def test_generic_suspicion_and_blind_approval_both_lose():
    world = SupplierInvoiceWorld()
    ev = world.cases(seed=5, n=800)
    approve_all = evaluate(world, lambda c: Proposal(APPROVE), ev, arm_name="aa")
    investigate_all = evaluate(world, lambda c: Proposal(REQUEST_EVIDENCE), ev, arm_name="ai")
    learned = evaluate(world, learned_arm(world, world.cases(seed=7, n=2000)), ev, arm_name="l")
    assert approve_all.mean_regret > learned.mean_regret * 10        # blind approval leaks catastrophically
    assert investigate_all.mean_regret > learned.mean_regret         # generic suspicion is taxed
    assert investigate_all.extra["wrongful_friction_rate"] > 0.9     # it investigates the legitimate ones too


def test_evidence_floor_is_real_and_learnable_gap_exists():
    world = SupplierInvoiceWorld()
    train = world.cases(seed=7, n=2000)
    ev = world.cases(seed=10_007, n=800)
    floor = evidence_floor_regret(world, ev, fit_cases=train)
    base = evaluate(world, control_arm(world), ev, arm_name="b")
    learned = evaluate(world, learned_arm(world, train), ev, arm_name="l")
    assert floor > 0                                                 # irreducible: undocumented-legit vs overcharge
    assert base.mean_regret > learned.mean_regret                    # a real learnable gap
    assert learned.mean_regret <= floor * 1.3                        # learning approaches the floor


# --------------------------------------------------------------------------- prospective-H machinery
def test_prediction_is_fixed_before_reveal():
    """The prediction must be computable without S1, and its ceiling follows the pre-registered rule."""
    world = SupplierInvoiceWorld()
    # deterministic stand-in prior = the naive baseline; S1 proxy = the learned policy
    pred = predict_learn_opportunity(world, control_arm(world), seed=7, n_eval=400, floor_fit_n=2000)
    assert pred.model_s1_regret is None                              # S1 not yet touched
    assert pred.systematic_misalignment >= 0
    if not pred.predict_null:
        expected_ceiling = pred.model_s0_regret - CAPTURE_THRESHOLD * pred.systematic_misalignment
        assert abs(pred.predicted_s1_ceiling - expected_ceiling) < 1e-6
    revealed = reveal_learn_outcome(pred, world, learned_arm(world, world.cases(seed=7, n=2000)),
                                    seed=7, n_eval=400)
    assert revealed.model_s1_regret is not None
    assert revealed.hypothesis_held is not None


def test_supplier_invoice_golden():
    world = SupplierInvoiceWorld()
    got = profile_world(world, seed=7, n_train=2000, n_eval=800).as_dict()
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n")
    assert got == json.loads(GOLDEN.read_text())
