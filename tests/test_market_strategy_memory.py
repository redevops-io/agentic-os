"""Market & Funnel Intelligence Phase 9 — strategy → outcome learning memory.

Proves: the three beliefs stay separate (market popularity never makes a strategy 'supported'); applicability is
derived ONLY from our own experiment verdicts; a contradicting outcome is remembered; INCONCLUSIVE adds no
signal; and prioritization prefers strategies we've validated when a pattern reappears.
"""
from __future__ import annotations

from agentic_os.market import StrategyMemory


def test_market_popularity_alone_is_untested():
    m = StrategyMemory()
    b = m.observe("pricing_frame", "daily_equivalent", entities=("e1", "e2", "e3"), market_survival_ms=94 * 86400000)
    assert b.cross_competitor_count == 3 and b.market_survival_ms > 0
    assert b.applicability == "UNTESTED" and not b.tested      # B1/B2 strong, but we haven't tested it → UNTESTED


def test_applicability_comes_only_from_our_outcomes():
    m = StrategyMemory()
    m.observe("pricing_frame", "daily_equivalent", entities=("e1", "e2"))
    m.record_outcome("pricing_frame", "daily_equivalent", "exp:1", "IMPROVED")
    b = m.belief("pricing_frame", "daily_equivalent")
    assert b.applicability == "SUPPORTED_FOR_OUR_FUNNEL" and b.improved == 1 and b.tested
    # B1/B2 preserved alongside B3 — not collapsed
    assert b.cross_competitor_count == 2


def test_contradiction_and_mixed_remembered():
    m = StrategyMemory()
    m.record_outcome("urgency", "countdown", "exp:a", "WORSE")
    assert m.belief("urgency", "countdown").applicability == "CONTRADICTED_FOR_OUR_FUNNEL"
    m.record_outcome("urgency", "countdown", "exp:b", "IMPROVED")
    assert m.belief("urgency", "countdown").applicability == "MIXED"


def test_inconclusive_adds_no_signal():
    m = StrategyMemory()
    m.record_outcome("guarantee", "30day", "exp:x", "INCONCLUSIVE")
    b = m.belief("guarantee", "30day")
    assert not b.tested and b.applicability == "UNTESTED" and b.improved == 0 and b.worse == 0


def test_guardrail_stop_counts_as_negative():
    m = StrategyMemory()
    m.record_outcome("discount", "bogo", "exp:g", "GUARDRAIL_STOP")
    assert m.belief("discount", "bogo").applicability == "CONTRADICTED_FOR_OUR_FUNNEL"


def test_prioritize_prefers_validated_then_convergent():
    m = StrategyMemory()
    m.record_outcome("pricing_frame", "daily_equivalent", "exp:1", "IMPROVED")   # SUPPORTED
    m.observe("cta", "start_free", entities=("e1", "e2", "e3"))                   # UNTESTED, 3 competitors
    m.observe("urgency", "countdown", entities=("e1",))                          # UNTESTED, 1 competitor
    m.record_outcome("bundling", "triple", "exp:2", "WORSE")                     # CONTRADICTED
    ranked = m.prioritize([("urgency", "countdown"), ("bundling", "triple"),
                           ("cta", "start_free"), ("pricing_frame", "daily_equivalent")])
    order = [key for key, _appl, _n in ranked]
    assert order[0] == ("pricing_frame", "daily_equivalent")     # SUPPORTED first
    assert order[1] == ("cta", "start_free")                     # UNTESTED, most convergent next
    assert order[-1] == ("bundling", "triple")                   # CONTRADICTED last
