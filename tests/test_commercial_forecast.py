"""Commercial pipeline forecast — reported-vs-verified weighting (plan §21).

Proves: an unverified high probability is capped in the COMMITTED total while still shown in the reported total;
a conflicted deal contributes nothing; a verified deal is trusted as-is; and the at-risk gap is reported.
"""
from __future__ import annotations

from agentic_os.commercial import FieldReadback, forecast_pipeline, produce_verified_deal
from agentic_os.deal_closing.contracts import Deal, Provenance


def _deal(ref, amount, prob, stage="proposal"):
    return Deal(prov=Provenance(provider="sf"), opportunity_ref=ref, reported_stage=stage,
                reported_amount_cents=amount, reported_probability=prob, source_systems=("sf",))


def test_unverified_high_probability_is_capped_in_committed_total():
    fc = forecast_pipeline([_deal("opp:a", 1_000_000, 0.9)])
    row = fc.deals[0]
    # reported trusts 0.9; committed caps to 0.5 (no read-back supports a 0.9 claim)
    assert row.reported_ev_cents == 900_000 and row.committed_ev_cents == 500_000
    assert "unverified_probability" in row.flags and row.supported is False
    assert fc.reported_total_cents == 900_000 and fc.committed_total_cents == 500_000
    assert fc.at_risk_cents == 400_000


def test_low_unverified_probability_is_trusted():
    # a modest reported probability with nothing contradicting it is 'supported' → not capped
    fc = forecast_pipeline([_deal("opp:b", 1_000_000, 0.3)])
    assert fc.deals[0].committed_ev_cents == 300_000 and fc.deals[0].supported is True
    assert fc.deals[0].flags == ()


def test_verified_probability_is_trusted_and_conflict_zeroes_out():
    base = _deal("opp:c", 2_000_000, 0.8)
    verified = produce_verified_deal(base, [
        FieldReadback("stage", "proposal", source="rb", verified_at=1000),
        FieldReadback("amount_cents", 2_000_000, source="erpnext", verified_at=1000)])
    fc_ok = forecast_pipeline([verified])
    assert fc_ok.deals[0].supported is True and fc_ok.deals[0].committed_ev_cents == 1_600_000

    conflicted = produce_verified_deal(base, [
        FieldReadback("amount_cents", 500_000, source="erpnext", verified_at=1000)])   # disagrees with reported
    fc_bad = forecast_pipeline([conflicted])
    assert "conflicted" in fc_bad.deals[0].flags and fc_bad.deals[0].committed_ev_cents == 0


def test_by_stage_and_order_are_deterministic():
    fc = forecast_pipeline([_deal("opp:z", 1_000_000, 0.2, stage="negotiation"),
                            _deal("opp:a", 1_000_000, 0.2, stage="proposal")])
    assert [d.opportunity_ref for d in fc.deals] == ["opp:a", "opp:z"]      # ordered by opportunity_ref
    assert set(fc.by_stage) == {"proposal", "negotiation"}
    assert forecast_pipeline([]).explanation == "No deals to forecast."
