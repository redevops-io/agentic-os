"""Safe-Learn abstention guard — deterministic machinery.

The live S0/S1/S2 held-out evaluation is recorded in SAFE_LEARN_RESULTS.md. These tests validate the guard,
the risk–coverage sweep, τ selection, S2 selection, and the loss diagnosis on deterministic proxies.
"""
from __future__ import annotations

from agentic_os.integrations.business.sim_lab.confirmation_battery_v2 import case_stakes
from agentic_os.integrations.business.sim_lab.harness import control_arm, learned_arm
from agentic_os.integrations.business.sim_lab.stale_quote_world import StaleQuoteWorld
from agentic_os.integrations.business.sim_lab.safe_learn import (
    AbstentionGuard, MATERIAL, diagnose_losses, evaluate_s2, lesson_reliability, risk_coverage,
    s2_normalized_regret, select_tau)


def _records(world, s0, s1, ev):
    recs = []
    for c in ev:
        stakes = case_stakes(world, c.latent) or 1.0
        optv = world.net_value(c.latent, world.optimal(c.latent))
        def rn(a):
            eff = a if world.admissible_action(c.observable, a) else world.default_hold()
            return max(0.0, optv - world.net_value(c.latent, eff)) / stakes
        recs.append({"bucket": world.bucket(c.observable), "s0_action": s0(c).action,
                     "s1_action": s1(c).action, "s0_rn": rn(s0(c).action), "s1_rn": rn(s1(c).action),
                     "prior_touches": int(c.observable.get("prior_touches", 0))})
    return recs


def _setup():
    w = StaleQuoteWorld()
    tr = w.cases(seed=7, n=2000)
    rel = lesson_reliability(w, tr)
    recs = _records(w, control_arm(w), learned_arm(w, tr), w.cases(seed=10_201, n=400))
    return w, tr, rel, recs


def test_reliability_is_a_probability_with_support():
    _, _, rel, _ = _setup()
    for br in rel.values():
        assert br.support >= 1 and 0.0 <= br.reliability <= 1.0


def test_guard_gates_on_support_and_reliability():
    _, _, rel, _ = _setup()
    b = next(iter(rel))
    lenient = AbstentionGuard(rel, min_support=1, tau=0.0)
    strict = AbstentionGuard(rel, min_support=10_000, tau=0.0)   # support floor no bucket meets
    assert lenient.accept(b) is True
    assert strict.accept(b) is False
    assert AbstentionGuard(rel, 1, 1.01).accept(b) is False       # reliability can't exceed 1


def test_risk_coverage_is_monotone_and_tau_selection_respects_the_bar():
    _, _, rel, recs = _setup()
    rc = risk_coverage(recs, rel, min_support=20, taus=[i / 10 for i in range(11)])
    covs = [r["coverage"] for r in rc]
    assert covs == sorted(covs, reverse=True)                     # coverage falls as τ rises
    tau = select_tau(rc, max_material_worse=0.05)
    g = AbstentionGuard(rel, 20, tau)
    worse = sum(1 for r in recs if s2_normalized_regret(r, g) - r["s0_rn"] > MATERIAL) / len(recs)
    assert worse <= 0.05 + 1e-9 or tau == max(r["tau"] for r in rc)


def test_s2_never_worse_material_rate_than_s1_and_is_selective():
    w, tr, rel, recs = _setup()
    g = AbstentionGuard(rel, 20, select_tau(risk_coverage(recs, rel, min_support=20,
                                                           taus=[i / 20 for i in range(21)])))
    ev = evaluate_s2(recs, g)
    assert ev["S2"]["material_worse_vs_s0"] <= ev["S1"]["material_worse_vs_s0"] + 1e-9
    # selective: the guard abstains on the higher would-be-harm subset
    assert ev["selectivity"]["harm_avoided_on_abstained"] >= ev["selectivity"]["harm_if_accepted"]


def test_diagnosis_partitions_losses():
    _, _, rel, recs = _setup()
    d = diagnose_losses(recs, rel, min_support=20)
    assert d["n_losses"] >= 0 and 0.0 <= d["abstainable_share"] <= 1.0
    assert set(d["by_category"]).issubset({
        "SUPPORT_WEAKNESS", "EVIDENCE_INSUFFICIENCY", "BUCKET_AGGREGATION", "TEMPORAL_MISMATCH",
        "ACTION_MISMATCH", "OUTCOME_VARIANCE", "CONTEXT_MISMATCH"})
