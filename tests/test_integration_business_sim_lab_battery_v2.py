"""v2 confirmation battery — deterministic machinery + gate logic (bounded normalized metric, safety, gate).

The live v2 run (frozen model) is recorded in BATTERY_RESULTS_v2.md, not asserted here.
"""
from __future__ import annotations

from agentic_os.integrations.business.sim_lab.confirmation_battery_v2 import (
    BATTERY_V2_VERSION, CAPTURE_THRESHOLD, EQUIVALENCE_MARGIN, MATERIAL_WORSENING, case_stakes, gate_v2,
    run_world_v2)
from agentic_os.integrations.business.sim_lab.harness import Proposal, control_arm, learned_arm
from agentic_os.integrations.business.sim_lab.fraud_world import fraud_world_a
from agentic_os.integrations.business.sim_lab.stale_quote_world import StaleQuoteWorld
from agentic_os.integrations.business.sim_lab.supplier_invoice_world import SupplierInvoiceWorld


def test_preregistered_constants():
    assert BATTERY_V2_VERSION == "prospective-h-battery/v2"
    assert CAPTURE_THRESHOLD == 0.40 and EQUIVALENCE_MARGIN == 0.03 and MATERIAL_WORSENING == 0.10


def test_case_stakes_positive_and_bounds_normalized_regret():
    world = SupplierInvoiceWorld()
    for c in world.cases(seed=1, n=200):
        s = case_stakes(world, c.latent)
        assert s >= 0
        # regret of any action is within [0, stakes] ⇒ normalized regret is in [0,1]
        opt = world.optimal(c.latent)
        for a in world.actions():
            reg = world.net_value(c.latent, opt) - world.net_value(c.latent, a)
            assert -1e-6 <= reg <= s + 1e-6


def test_v2_normalized_metrics_are_bounded_and_paired():
    """Deterministic machinery check: baseline as S0 proxy, per-seed learned as S1 proxy."""
    world = SupplierInvoiceWorld()
    s0 = control_arm(world)
    s1_for_seed = lambda seed: learned_arm(world, world.cases(seed=seed, n=2000))
    r = run_world_v2(world, s0, s1_for_seed, seeds=(101, 103), n_eval=200, floor_fit_n=2000)
    assert r.n_pairs == 400
    for v in (r.normalized_floor, r.normalized_s0, r.normalized_s1):
        assert 0.0 <= v <= 1.0                               # bounded ⇒ stable
    assert abs((r.win_rate + r.tie_rate + r.loss_rate) - 1.0) < 1e-9
    # learned (S1 proxy) should not be worse than baseline (S0 proxy) on average here
    assert r.mean_delta >= -1e-6
    assert r.normalized_s1 <= r.normalized_floor + 0.05      # learned approaches the floor


def test_v2_gate_flags_capture_miss_null_and_safety():
    from agentic_os.integrations.business.sim_lab.confirmation_battery_v2 import WorldV2Result
    # a misaligned world that failed capture
    miss = WorldV2Result(
        world_id="w_miss", n_pairs=900, normalized_floor=0.05, normalized_s0=0.40, normalized_s1=0.35,
        normalized_systematic=0.35, normalized_lift=0.05, capture=0.14, mean_delta=0.05,
        median_delta=0.0, delta_ci=(0.02, 0.08), win_rate=0.3, tie_rate=0.5, loss_rate=0.2,
        material_worse_rate=0.01, mean_downside_given_worse=-0.05, p95_worsening=0.1, p99_worsening=0.2,
        raw_s0=0, raw_s1=0, is_misaligned=True, capture_pass=False, equivalence_pass=None, safety_pass=True)
    # a null world that is NOT equivalent (CI exceeds margin)
    noneq = WorldV2Result(
        world_id="w_noneq", n_pairs=900, normalized_floor=0.30, normalized_s0=0.33, normalized_s1=0.29,
        normalized_systematic=0.03, normalized_lift=0.04, capture=-1.0, mean_delta=0.04,
        median_delta=0.0, delta_ci=(0.02, 0.06), win_rate=0.2, tie_rate=0.6, loss_rate=0.2,
        material_worse_rate=0.01, mean_downside_given_worse=-0.02, p95_worsening=0.05, p99_worsening=0.1,
        raw_s0=0, raw_s1=0, is_misaligned=False, capture_pass=None, equivalence_pass=False, safety_pass=True)
    # a safety breach
    unsafe = WorldV2Result(
        world_id="w_unsafe", n_pairs=900, normalized_floor=0.05, normalized_s0=0.40, normalized_s1=0.20,
        normalized_systematic=0.35, normalized_lift=0.20, capture=0.57, mean_delta=0.20,
        median_delta=0.1, delta_ci=(0.15, 0.25), win_rate=0.6, tie_rate=0.2, loss_rate=0.2,
        material_worse_rate=0.12, mean_downside_given_worse=-0.3, p95_worsening=0.5, p99_worsening=0.8,
        raw_s0=0, raw_s1=0, is_misaligned=True, capture_pass=True, equivalence_pass=None, safety_pass=False)
    v = gate_v2([miss, noneq, unsafe])
    assert v["survives"] is False
    assert any("CAPTURE MISS" in f for f in v["falsifiers"])
    assert any("NULL NON-EQUIVALENCE" in f for f in v["falsifiers"])
    assert any("SAFETY BREACH" in f for f in v["falsifiers"])
