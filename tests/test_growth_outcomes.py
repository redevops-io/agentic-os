"""Per-scope learning (Growth Intelligence outcome loop). Offline.

Outcomes (approve/dismiss + worked/didn't) per scope → per-action-kind priority weights that re-order the
queue; scoped so one site's history never re-weights another's; smoothed so one outcome barely moves it.
"""
from __future__ import annotations

from agentic_os.growth.outcomes import (
    Decision, InMemoryOutcomeLedger, Outcome, Result, apply_weights, scope_weights,
)


def _ledger(*rows):
    led = InMemoryOutcomeLedger()
    for r in rows:
        led.record(r)
    return led


def test_no_evidence_is_neutral_weight():
    assert scope_weights(InMemoryOutcomeLedger(), "site") == {}
    # apply_weights with no weights leaves priority unchanged (weight 1.0)
    rows = [{"action_kind": "CTR_OPPORTUNITY", "priority": 0.4, "goal_aligned": True}]
    out = apply_weights(rows, {})
    assert out[0]["learned_weight"] == 1.0 and out[0]["learned_priority"] == 0.4


def test_approved_and_worked_boosts_dismissed_damps():
    led = _ledger(
        Outcome("s", "CTR_OPPORTUNITY", Decision.APPROVED, Result.SUCCEEDED),
        Outcome("s", "CTR_OPPORTUNITY", Decision.APPROVED, Result.SUCCEEDED),
        Outcome("s", "HIGH_TRAFFIC_LEVERAGE", Decision.DISMISSED),
        Outcome("s", "HIGH_TRAFFIC_LEVERAGE", Decision.DISMISSED),
    )
    w = scope_weights(led, "s")
    assert w["CTR_OPPORTUNITY"] > 1.0 > w["HIGH_TRAFFIC_LEVERAGE"]     # boosted vs damped
    assert 0.5 <= w["HIGH_TRAFFIC_LEVERAGE"] and w["CTR_OPPORTUNITY"] <= 1.5


def test_learning_is_scoped_per_site():
    led = _ledger(Outcome("siteA", "CTR_OPPORTUNITY", Decision.DISMISSED),
                  Outcome("siteA", "CTR_OPPORTUNITY", Decision.DISMISSED))
    assert "CTR_OPPORTUNITY" in scope_weights(led, "siteA")
    assert scope_weights(led, "siteB") == {}                          # other site unaffected


def test_apply_weights_reorders_by_learned_priority():
    led = _ledger(
        Outcome("s", "LEAD_INTENT", Decision.APPROVED, Result.SUCCEEDED),
        Outcome("s", "LEAD_INTENT", Decision.APPROVED, Result.SUCCEEDED),
        Outcome("s", "CTR_OPPORTUNITY", Decision.DISMISSED),
        Outcome("s", "CTR_OPPORTUNITY", Decision.DISMISSED),
    )
    rows = [{"action_kind": "CTR_OPPORTUNITY", "priority": 0.42, "goal_aligned": True, "candidate_id": "c"},
            {"action_kind": "LEAD_INTENT", "priority": 0.40, "goal_aligned": True, "candidate_id": "l"}]
    out = apply_weights(rows, scope_weights(led, "s"))
    # LEAD_INTENT started lower but learning (approved+worked) lifts it above the dismissed CTR action
    assert out[0]["action_kind"] == "LEAD_INTENT"


def test_smoothing_one_outcome_barely_moves_weight():
    w = scope_weights(_ledger(Outcome("s", "NEAR_WIN", Decision.APPROVED, Result.SUCCEEDED)), "s")
    assert 1.0 < w["NEAR_WIN"] < 1.25                                 # single positive → small nudge
