"""Adherence diagnosis machinery — the 2×2 classification and the enforce-lesson counterfactual."""
from __future__ import annotations

from agentic_os.integrations.business.sim_lab.adherence import classify_adherence


def _rec(s0, s1, lesson_rn, s1_act, lesson_act):
    return {"s0_rn": s0, "s1_rn": s1, "lesson_rn": lesson_rn, "s1_action": s1_act,
            "lesson_action": lesson_act}


def test_cells_and_counterfactual():
    recs = [
        # ADHERED + GOOD: followed lesson, fine
        _rec(0.5, 0.1, 0.1, "A", "A"),
        # ADHERED + BAD: followed lesson, still materially worse than S0 (lesson/aggregation limit)
        _rec(0.1, 0.4, 0.4, "A", "A"),
        # DEVIATED + BAD, avoidable: model chose worse than the lesson would have
        _rec(0.2, 0.6, 0.2, "B", "A"),      # s1_rn - lesson_rn = 0.4 avoidable
        # DEVIATED + GOOD, justified override: model beat the coarse lesson
        _rec(0.5, 0.1, 0.5, "B", "A"),      # lesson_rn - s1_rn = 0.4 override value
    ]
    r = classify_adherence(recs)
    assert r.n_accepted == 4
    assert r.adhered_good == 1 and r.adhered_bad == 1
    assert r.deviated_bad == 1 and r.deviated_good == 1
    assert r.deviation_rate == 0.5
    assert abs(r.avoidable_regret - 0.4 / 4) < 1e-9      # one avoidable deviation of 0.4, averaged over 4
    assert abs(r.override_value - 0.4 / 4) < 1e-9        # one justified override worth 0.4
    assert abs(r.net_adherence_gain - 0.0) < 1e-9        # here they cancel → forcing the lesson is neutral


def test_enforce_material_worse_counts_lesson_not_model():
    # a case where enforcing the lesson would be materially worse than S0, but the model (S1) was fine
    recs = [_rec(0.1, 0.1, 0.5, "B", "A")]               # lesson_rn - s0_rn = 0.4 > 0.10 ⇒ enforce worse
    r = classify_adherence(recs)
    assert r.enforce_material_worse_vs_s0 == 1.0
    assert r.s1_material_worse_vs_s0 == 0.0              # the model itself did not worsen vs S0
