"""Prospective-H confirmation battery — deterministic machinery + gate logic.

The live battery (frozen model) is non-deterministic and recorded in BATTERY_RESULTS_v1.md, not asserted here.
These tests validate that the battery fixes each prediction before the reveal, aggregates per-seed correctly,
and that the pre-registered gate classifies worlds and detects the two falsifiers.
"""
from __future__ import annotations

from dataclasses import replace

from agentic_os.integrations.business.sim_lab.confirmation_battery import (
    BATTERY_VERSION, BatterySpec, WorldBatteryResult, deterministic_battery, gate_verdict)
from agentic_os.integrations.business.sim_lab.fraud_world import fraud_world_a
from agentic_os.integrations.business.sim_lab.supplier_invoice_world import SupplierInvoiceWorld


def test_spec_is_frozen_and_preregistered():
    spec = BatterySpec()
    assert spec.version == BATTERY_VERSION
    assert spec.seeds == (11, 23, 37, 53, 71)
    assert spec.capture_threshold == 0.40 and spec.negligible_systematic_share == 0.15
    assert spec.seed_pass_fraction == 0.80


def test_deterministic_battery_runs_all_seeds_and_holds():
    spec = BatterySpec(seeds=(11, 23, 37), n_eval=200)
    r = deterministic_battery(SupplierInvoiceWorld(), spec)
    assert r.n_seeds == 3 and len(r.per_seed) == 3
    # each per-seed record carries a prediction that was fixed before the reveal
    for rec in r.per_seed:
        assert "predicted_s1_ceiling" in rec and rec["model_s1_regret"] is not None
    assert r.passed and r.held_fraction >= 0.8


def test_gate_survives_when_all_worlds_pass():
    spec = BatterySpec(seeds=(11, 23, 37), n_eval=200)
    rs = [deterministic_battery(w, spec) for w in (fraud_world_a(), SupplierInvoiceWorld())]
    v = gate_verdict(rs, spec)
    assert v["survives"] is True and v["falsifiers"] == []


def test_gate_detects_a_capture_miss_falsifier():
    spec = BatterySpec()
    # a synthetic large-gap (positive-prediction) world that failed to capture
    miss = WorldBatteryResult(
        world_id="synthetic_large_gap", n_seeds=5, mean_floor=100, mean_s0=8000, sd_s0=100,
        mean_systematic=7900, mean_s1=7000, sd_s1=100, predict_null_fraction=0.0, held_fraction=0.2,
        mean_capture=0.1, per_seed=(), passed=False)
    v = gate_verdict([miss], spec)
    assert v["survives"] is False
    assert any("CAPTURE MISS" in f for f in v["falsifiers"])


def test_gate_detects_a_null_control_breach():
    spec = BatterySpec()
    breach = WorldBatteryResult(
        world_id="synthetic_null_world", n_seeds=5, mean_floor=500, mean_s0=520, sd_s0=20,
        mean_systematic=20, mean_s1=100, sd_s1=20, predict_null_fraction=1.0, held_fraction=0.2,
        mean_capture=-1.0, per_seed=(), passed=False)
    v = gate_verdict([breach], spec)
    assert v["survives"] is False
    assert any("NULL-CONTROL BREACH" in f for f in v["falsifiers"])
