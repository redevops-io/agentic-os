"""Fraud A→B transfer, the prior-misalignment / evidence-floor instrument, and the Lesson object.

Deterministic and reproducible. The live frozen-model H-table is exploratory (see BENCHMARK_PROVENANCE.md)
and is not asserted here; what is asserted is the model-free structure H relies on — the evidence floor, the
misalignment decomposition, same-family policy transfer, and the Lesson's recorded boundary conditions.
"""
from __future__ import annotations

import json
import os
import pathlib

from agentic_os.integrations.business.sim_lab import (
    FRAUD_A, FRAUD_B, RECEIVABLES_INTERVENTION_LESSON, evidence_floor_regret, fraud_world_a,
    fraud_world_b, measure_misalignment, profile_world, run_fraud_transfer)
from agentic_os.integrations.business.sim_lab.harness import control_arm
from agentic_os.integrations.business.sim_lab.stale_quote_world import StaleQuoteWorld

GOLDEN = pathlib.Path(__file__).parent / "golden" / "sim_lab_transfer_axes.json"


# --------------------------------------------------------------------------- Fraud B is a real shift
def test_fraud_b_is_a_genuinely_different_business():
    assert FRAUD_B.world_id != FRAUD_A.world_id
    assert FRAUD_B.base_fraud_rate > FRAUD_A.base_fraud_rate            # more fraud
    assert FRAUD_B.hold_complete_fraud > FRAUD_A.hold_complete_fraud    # instant delivery
    # noisier signals: fraud/legit separations are smaller in B than in A on average
    def mean_sep(p):
        return sum(pf - pl for _, pf, pl in p.signal_probs) / len(p.signal_probs)
    assert mean_sep(FRAUD_B) < mean_sep(FRAUD_A)


def test_fraud_b_has_a_higher_evidence_floor_than_fraud_a():
    a, b = fraud_world_a(), fraud_world_b()
    fa = evidence_floor_regret(a, a.cases(seed=99, n=1500))
    fb = evidence_floor_regret(b, b.cases(seed=99, n=1500))
    assert fb > fa                                                     # noisier signals ⇒ higher floor


# --------------------------------------------------------------------------- same-family policy transfer
def test_fraud_A_policy_transfers_directly_to_B():
    """Same family, shifted distribution: A's learned policy applied to B should clearly beat B's baseline and
    land close to B-native — direct POLICY transfer, unlike the cross-domain case where only principle moved."""
    rep = run_fraud_transfer()
    base, native, direct, adapt = (rep["B_baseline"], rep["B_native"], rep["A_to_B_direct"],
                                   rep["A_to_B_adapt"])
    assert direct.mean_regret < base.mean_regret * 0.6                 # transfers a lot
    assert direct.mean_regret <= native.mean_regret * 1.5             # within reach of native
    assert adapt.mean_regret <= native.mean_regret + 1                # adaptation recovers native


# --------------------------------------------------------------------------- misalignment instrument
def test_evidence_floor_separates_worlds():
    fraud = fraud_world_a()
    sq = StaleQuoteWorld()
    ff = evidence_floor_regret(fraud, fraud.cases(seed=99, n=1500))
    sf = evidence_floor_regret(sq, sq.cases(seed=99, n=1500))
    assert ff >= 0 and sf > ff                                        # stale-quote has real irreducible uncertainty


def test_misalignment_decomposition_is_bounded():
    world = fraud_world_a()
    ev = world.cases(seed=123, n=600)
    m = measure_misalignment(world, control_arm(world), ev, prior_label="baseline")
    assert m.systematic_misalignment >= 0
    assert m.systematic_misalignment <= m.prior_regret + 1e-6         # systematic can't exceed total regret
    assert 0.0 <= m.learnable_share <= 1.0
    # systematic + floor reconstructs the prior regret (within rounding)
    assert abs((m.systematic_misalignment + m.evidence_floor_regret) - m.prior_regret) < 1.0


# --------------------------------------------------------------------------- Lesson object
def test_receivables_lesson_records_its_boundary_conditions():
    lesson = RECEIVABLES_INTERVENTION_LESSON
    joined = " ".join(lesson.known_counterexamples).lower()
    assert "active-close" in joined                                   # restraint stops where positive action is required
    assert "evidence insufficiency" in joined                         # dead-vs-patient is not a learning failure
    assert "readiness" in lesson.principle.lower()
    # the transferable core carries the principle but drops the collections-specific policy
    core = lesson.transferable_core()
    assert core.principle == lesson.principle and core.local_policy == {}


# --------------------------------------------------------------------------- golden
def test_transfer_axes_golden():
    got = {
        "fraud_A_to_B": {k: {"mean_regret": round(v.mean_regret, 2),
                             "extra": {m: round(x, 4) for m, x in v.extra.items()}}
                         for k, v in run_fraud_transfer().items()},
        "profiles": {w.world_id: profile_world(w).as_dict()
                     for w in (fraud_world_a(), fraud_world_b(), StaleQuoteWorld())},
    }
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n")
    assert got == json.loads(GOLDEN.read_text())
