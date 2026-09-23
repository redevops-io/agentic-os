"""Adherence diagnosis — the second condition for safe learning.

Safe-Learn (S2) established condition (1): SHOULD a lesson apply? (support/applicability guard). But 16/92
Stale-Quote losses were cases where a *supported* lesson was ACCEPTED and the frozen model still chose an
action inconsistent with it. That is condition (2): WILL the model actually follow a supported lesson?

This module characterises ACTION_MISMATCH *before* proposing any mechanism. For every accepted lesson
application it classifies the 2×2:

    SUPPORTED + ADHERED + GOOD      model followed the lesson, decision fine
    SUPPORTED + ADHERED + BAD       followed the lesson, still bad (lesson/aggregation limit, not adherence)
    SUPPORTED + DEVIATED + GOOD     model overrode the lesson and was RIGHT (case-specific evidence — justified)
    SUPPORTED + DEVIATED + BAD      model overrode the lesson and was WRONG (avoidable if it had adhered)

and computes the counterfactual of *enforcing* the lesson action. Two quantities decide whether adherence
matters, and they trade off:

  * ``avoidable_regret``  — normalized regret that enforcing the lesson would REMOVE (deviations where the
    lesson action was better). If large, ACTION_MISMATCH is a real, addressable failure.
  * ``override_value``    — normalized regret that enforcing the lesson would ADD (deviations where the
    model's own choice beat the coarse lesson — justified overrides). If comparable to avoidable_regret,
    forcing the lesson is the wrong design: lessons must inform, not become authority.

H4 (adherence): once a lesson is applicable and sufficiently supported, explicit lesson-policy conflict
handling reduces avoidable deviations WITHOUT suppressing justified case-specific overrides.
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Sequence

MATERIAL = 0.10


@dataclass(frozen=True)
class AdherenceReport:
    n_accepted: int
    adhered_good: int
    adhered_bad: int
    deviated_good: int
    deviated_bad: int
    deviation_rate: float
    # counterfactual of enforcing the lesson action on accepted cases
    avoidable_regret: float          # mean normalized regret removed by adherence (lesson better than model)
    override_value: float            # mean normalized regret added by adherence (model beat lesson)
    net_adherence_gain: float        # avoidable_regret − override_value (per accepted case)
    enforce_material_worse_vs_s0: float   # material-worse(enforced-lesson vs S0) — the safety of forcing
    s1_material_worse_vs_s0: float        # for comparison (what S1/S2 actually did)

    def as_dict(self) -> dict:
        r = lambda x: round(x, 4)
        return {"n_accepted": self.n_accepted,
                "cells": {"ADHERED_GOOD": self.adhered_good, "ADHERED_BAD": self.adhered_bad,
                          "DEVIATED_GOOD": self.deviated_good, "DEVIATED_BAD": self.deviated_bad},
                "deviation_rate": r(self.deviation_rate),
                "avoidable_regret": r(self.avoidable_regret), "override_value": r(self.override_value),
                "net_adherence_gain": r(self.net_adherence_gain),
                "enforce_material_worse_vs_s0": r(self.enforce_material_worse_vs_s0),
                "s1_material_worse_vs_s0": r(self.s1_material_worse_vs_s0)}


def classify_adherence(records: Sequence[Mapping], *, material: float = MATERIAL) -> AdherenceReport:
    """``records`` must be *accepted* cases enriched with: s0_rn, s1_rn, lesson_rn, s1_action, lesson_action."""
    accepted = list(records)
    n = len(accepted) or 1
    cells: Counter = Counter()
    avoidable, override = [], []
    enforce_worse = s1_worse = 0
    for r in accepted:
        adhered = r["s1_action"] == r["lesson_action"]
        bad = (r["s1_rn"] - r["s0_rn"]) > material
        cells[("ADHERED" if adhered else "DEVIATED", "BAD" if bad else "GOOD")] += 1
        if not adhered:
            d = r["s1_rn"] - r["lesson_rn"]          # >0: lesson better (avoidable) ; <0: model better (override)
            (avoidable if d > 0 else override).append(abs(d))
        if (r["lesson_rn"] - r["s0_rn"]) > material:  # would enforcing the lesson be materially worse than S0?
            enforce_worse += 1
        if (r["s1_rn"] - r["s0_rn"]) > material:
            s1_worse += 1
    avoid_sum = sum(avoidable)
    override_sum = sum(override)
    return AdherenceReport(
        n_accepted=len(accepted),
        adhered_good=cells[("ADHERED", "GOOD")], adhered_bad=cells[("ADHERED", "BAD")],
        deviated_good=cells[("DEVIATED", "GOOD")], deviated_bad=cells[("DEVIATED", "BAD")],
        deviation_rate=sum(1 for r in accepted if r["s1_action"] != r["lesson_action"]) / n,
        avoidable_regret=avoid_sum / n, override_value=override_sum / n,
        net_adherence_gain=(avoid_sum - override_sum) / n,
        enforce_material_worse_vs_s0=enforce_worse / n, s1_material_worse_vs_s0=s1_worse / n)
