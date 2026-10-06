"""Deal Closing Intelligence Phase 1 — state substrate.

Proves the core product distinction (CRM-REPORTED vs RUNTIME-VERIFIED) is first-class and honest: a reported
value is not treated as verified; a disagreement surfaces as CONFLICTED and is never merged; a stale verification
is not trusted as current; and UNKNOWN (never assessed) is kept distinct from UNSATISFIED (assessed, not met).
"""
from __future__ import annotations

from agentic_os.deal_closing import (
    BuyingCommittee, Claim, ClaimStatus, CommitteeMember, CommitteeRole, ConditionState, Deal, DealCondition,
    DealEvidence, EvidenceSignal, NORMALIZED_CONDITIONS, RoleStatus, assess_condition, derive_state,
)
from agentic_os.deal_closing.contracts import Provenance

_DAY = 86_400_000
NOW = 1_700_000_000_000


def _prov(provider="salesforce"):
    return Provenance(provider=provider)


# ── reported vs verified ────────────────────────────────────────────────────────────────────────────
def test_reported_is_not_verified_until_read_back():
    c = Claim(field_name="stage", reported="Negotiation", reported_source="salesforce")
    assert c.status() is ClaimStatus.UNVERIFIED
    verified = Claim(field_name="stage", reported="Negotiation", reported_source="salesforce",
                     verified="Negotiation", verified_source="runtime", verified_at=NOW)
    assert verified.status() is ClaimStatus.VERIFIED


def test_disagreement_is_conflicted_never_merged():
    c = Claim(field_name="amount_cents", reported=5_000_00, reported_source="salesforce",
              verified=2_000_00, verified_source="erpnext", verified_at=NOW)
    assert c.status() is ClaimStatus.CONFLICTED


def test_verification_goes_stale():
    old = Claim(field_name="close_date", reported="2026-10-31", verified="2026-10-31",
                verified_source="runtime", verified_at=NOW - 40 * _DAY)
    assert old.status(now=NOW, max_age_ms=30 * _DAY) is ClaimStatus.STALE
    assert old.status(now=NOW, max_age_ms=60 * _DAY) is ClaimStatus.VERIFIED


def test_deal_surfaces_unverified_and_conflicts():
    deal = Deal(
        prov=_prov(), tenant="t1", account_ref="acct:acme", opportunity_ref="sf:006x",
        reported_stage="Negotiation", reported_amount_cents=5_000_00, currency="USD",
        reported_probability=0.8, source_systems=("salesforce",),
        claims=(
            Claim(field_name="stage", reported="Negotiation", verified="Negotiation",
                  verified_source="runtime", verified_at=NOW),
            Claim(field_name="amount_cents", reported=5_000_00, verified=2_000_00, verified_source="erpnext",
                  verified_at=NOW),                               # conflict
            Claim(field_name="economic_buyer", reported="verified", reported_source="salesforce"),  # unverified
        ),
    )
    assert "amount_cents" in deal.unverified_fields()
    assert "economic_buyer" in deal.unverified_fields()
    assert "stage" not in deal.unverified_fields()
    assert len(deal.conflicts()) == 1
    # an 80% reported probability is NOT supported when a field conflicts and another is unverified
    assert deal.reported_probability_supported() is False


def test_high_probability_with_no_verification_is_unsupported():
    deal = Deal(prov=_prov(), reported_probability=0.8)  # CRM claims 80%, zero verification
    assert deal.reported_probability_supported() is False


def test_fully_verified_probability_supported():
    deal = Deal(prov=_prov(), reported_probability=0.8,
                claims=(Claim(field_name="stage", reported="Negotiation", verified="Negotiation",
                              verified_source="runtime", verified_at=NOW),))
    assert deal.reported_probability_supported(now=NOW, max_age_ms=60 * _DAY) is True


# ── condition state ─────────────────────────────────────────────────────────────────────────────────
def test_unknown_is_distinct_from_unsatisfied():
    unknown, _ = derive_state(())
    assert unknown is ConditionState.UNKNOWN
    unsat, _ = derive_state((EvidenceSignal(supports=False, confidence=0.9, ref="e1"),))
    assert unsat is ConditionState.UNSATISFIED


def test_condition_conflict_not_averaged():
    state, conf = derive_state((
        EvidenceSignal(supports=True, confidence=0.8, ref="pro"),
        EvidenceSignal(supports=False, confidence=0.7, ref="con"),
    ))
    assert state is ConditionState.CONFLICTED
    assert conf > 0


def test_condition_satisfied_and_partial():
    sat, _ = derive_state((EvidenceSignal(supports=True, confidence=0.9, ref="a"),))
    assert sat is ConditionState.SATISFIED
    part, _ = derive_state((EvidenceSignal(supports=True, confidence=0.4, ref="a"),))
    assert part is ConditionState.PARTIAL


def test_assess_condition_rejects_unknown_name():
    try:
        assess_condition("deal:1", "NOT_A_REAL_CONDITION", ())
        assert False, "should have raised"
    except ValueError:
        pass


def test_assess_condition_freshness_demotes_to_stale():
    sig = (EvidenceSignal(supports=True, confidence=0.9, observed_ms=NOW - 30 * _DAY, ref="e1"),)
    cond = assess_condition("deal:1", "SIGNATURE_PENDING", sig, now=NOW)  # 7-day window
    assert cond.state is ConditionState.STALE
    fresh = assess_condition("deal:1", "SECURITY_REVIEW_COMPLETE", sig, now=NOW)  # 60-day default
    assert fresh.state is ConditionState.SATISFIED


def test_all_normalized_conditions_unique():
    assert len(NORMALIZED_CONDITIONS) == len(set(NORMALIZED_CONDITIONS))
    assert len(NORMALIZED_CONDITIONS) == 22


# ── buying committee ────────────────────────────────────────────────────────────────────────────────
def test_committee_missing_roles_only_counts_verified_as_covered():
    committee = BuyingCommittee(
        prov=_prov(), deal_ref="deal:1",
        members=(
            CommitteeMember(entity_ref="e:alice", name="Alice", role=CommitteeRole.CHAMPION,
                            role_status=RoleStatus.VERIFIED, confidence=0.9),
            CommitteeMember(entity_ref="e:bob", name="Bob", role=CommitteeRole.ECONOMIC_BUYER,
                            role_status=RoleStatus.PROBABLE, confidence=0.5),  # not confirmed
        ),
    )
    missing = committee.missing_roles((CommitteeRole.CHAMPION, CommitteeRole.ECONOMIC_BUYER,
                                       CommitteeRole.SECURITY))
    assert CommitteeRole.CHAMPION not in missing            # verified → covered
    assert CommitteeRole.ECONOMIC_BUYER in missing          # only probable → still missing
    assert CommitteeRole.SECURITY in missing                # absent → missing


def test_two_economic_buyers_conflict():
    committee = BuyingCommittee(
        prov=_prov(), deal_ref="deal:1",
        members=(
            CommitteeMember(entity_ref="e:bob", role=CommitteeRole.ECONOMIC_BUYER,
                            role_status=RoleStatus.VERIFIED, confidence=0.9),
            CommitteeMember(entity_ref="e:carol", role=CommitteeRole.ECONOMIC_BUYER,
                            role_status=RoleStatus.VERIFIED, confidence=0.8),
        ),
    )
    assert committee.role_status(CommitteeRole.ECONOMIC_BUYER) is RoleStatus.CONFLICTED


# ── BusinessObject discipline ───────────────────────────────────────────────────────────────────────
def test_deal_is_content_addressed_and_provenanced():
    d1 = Deal(prov=_prov("salesforce"), opportunity_ref="006x", reported_amount_cents=100)
    d2 = Deal(prov=_prov("hubspot"), opportunity_ref="006x", reported_amount_cents=100)
    # same business facts from different providers → identical digest (provenance excluded)
    assert d1.digest() == d2.digest()
    d3 = Deal(prov=_prov("salesforce"), opportunity_ref="006x", reported_amount_cents=200)
    assert d1.digest() != d3.digest()
    assert d1.to_dict()["kind"] == "deal_closing.deal"


def test_evidence_object():
    ev = DealEvidence(prov=_prov("zendesk"), deal_ref="deal:1", kind_of="support.case",
                      summary="blocker ticket", supports="SECURITY_REVIEW_COMPLETE", confidence=0.7)
    assert ev.to_dict()["supports"] == "SECURITY_REVIEW_COMPLETE"
    assert isinstance(DealCondition(prov=_prov(), deal_ref="d", name="PRICING_AGREED").digest(), str)
