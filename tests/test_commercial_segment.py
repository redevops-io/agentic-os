"""Commercial account segmentation — deterministic value tiers that refuse to high-tier unresolved identity.

Proves: value-tier boundaries; health/inactive flags; and that an unresolved or low-confidence identity is
marked UNVERIFIED rather than silently placed in a value tier.
"""
from __future__ import annotations

from agentic_os.commercial import AccountSignal, Segment, segment_accounts
from agentic_os.commercial.identity import CanonicalOrganization
from agentic_os.integration.contracts import ResolutionStatus


def _org(entity_id, name="", status=ResolutionStatus.RESOLVED, confidence=0.9):
    return CanonicalOrganization.build(name=name or entity_id, resolution_status=status,
                                       confidence=confidence, entity_id=entity_id)


def test_value_tiers_and_health_flag():
    accts = [_org("acme"), _org("beta"), _org("gamma"), _org("delta")]
    signals = {
        "acme": AccountSignal(annual_value_cents=12_000_000, health=0.9),    # strategic
        "beta": AccountSignal(annual_value_cents=3_000_000, health=0.3),     # growth + at-risk health
        "gamma": AccountSignal(annual_value_cents=800_000, health=0.8),      # core
        "delta": AccountSignal(annual_value_cents=100_000, health=0.8),      # longtail
    }
    res = segment_accounts(accts, signals=signals)
    by_id = {a.entity_id: a for a in res.assignments}
    assert by_id["acme"].tier is Segment.STRATEGIC
    assert by_id["beta"].tier is Segment.GROWTH and "at_risk_health" in by_id["beta"].flags
    assert by_id["gamma"].tier is Segment.CORE
    assert by_id["delta"].tier is Segment.LONGTAIL
    assert res.by_tier[Segment.STRATEGIC.value] == ("acme",)


def test_unresolved_or_low_confidence_identity_is_not_high_tiered():
    accts = [_org("ghost", status=ResolutionStatus.UNRESOLVED, confidence=0.0),
             _org("fuzzy", status=ResolutionStatus.RESOLVED, confidence=0.2)]
    signals = {"ghost": AccountSignal(annual_value_cents=50_000_000, health=1.0),   # huge value, but unresolved
               "fuzzy": AccountSignal(annual_value_cents=50_000_000, health=1.0)}   # huge value, low confidence
    res = segment_accounts(accts, signals=signals, confidence_floor=0.5)
    by_id = {a.entity_id: a for a in res.assignments}
    assert by_id["ghost"].tier is Segment.UNVERIFIED and "unresolved_identity" in by_id["ghost"].flags
    assert by_id["fuzzy"].tier is Segment.UNVERIFIED and "low_resolution_confidence" in by_id["fuzzy"].flags


def test_defaults_and_determinism():
    # missing signal → zero value (longtail) + unhealthy flag; ordering by entity_id
    res = segment_accounts([_org("zeta"), _org("alpha")])
    assert [a.entity_id for a in res.assignments] == ["alpha", "zeta"]
    assert all(a.tier is Segment.LONGTAIL and "at_risk_health" in a.flags for a in res.assignments)
    assert segment_accounts([]).explanation == "No accounts to segment."
