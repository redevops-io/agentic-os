"""Governed sponsorship booking (Phases 2-8): quote → normalize → approve(ceiling) → HARD-gated booking →
verified creative → attribution → cross-channel. Offline; no real email, no real spend (all SIMULATED)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_contracts import AuthorityContext, PrincipalRef  # noqa: E402
from agentic_os.world import (  # noqa: E402
    ALL_WORLDS,
    Claim,
    GovernedSponsorshipWorld,
    ScenarioOrchestrator,
    build_attribution,
    build_creative_brief,
    evaluate_booking,
    normalize_quote,
)
from agentic_os.world.sponsorship import PriceBasis  # noqa: E402
from agentic_os.world.sponsorship_economics import SponsorshipApproval, SponsorshipQuote  # noqa: E402


_APPROVE = {"POLICY_APPROVAL": "approve within ceiling"}


def _auth():
    return AuthorityContext(authority_id="c", principal=PrincipalRef(id="f", tenant="rd"), purpose="s", scope=())


def _run(**kw):
    return ScenarioOrchestrator().run(GovernedSponsorshipWorld(), seed="s1", authority=_auth(),
                                      offline=True, **kw)


# ------------------------------------------------------------------- integration: the governed happy path
def test_approved_run_books_simulated_within_ceiling_and_attributes():
    o = _run(answers=_APPROVE).outcome
    assert o["approved"] is True and o["booked"] is True
    assert o["spend_committed_simulated"] is True and o["committed_amount"] <= o["ceiling"]
    assert o["quote"]["basis"] == PriceBasis.DIRECT_QUOTE           # booked on a real quote, never an estimate
    assert o["attribution_complete"] is True
    assert o["mode"] == "SIMULATE"                                  # no real money moved


def test_all_governance_invariants_are_zero():
    o = _run(answers=_APPROVE).outcome
    assert o["governance"] == {"commit_without_approval": False, "spend_over_ceiling": False,
                               "wrong_venue_authorized": False, "expired_approval_used": False,
                               "estimate_booked_as_quote": False, "unsupported_claim_aired": False}
    assert _run(answers=_APPROVE).metrics.verified is True          # ground truth passed via orchestrator


def test_unsupported_creative_claim_never_airs():
    o = _run(answers=_APPROVE).outcome
    cr = o["creative"]
    assert cr["rejected_claims"]                                    # the "10x faster than everyone" claim
    assert all(c not in cr["aired_claims"] for c in cr["rejected_claims"])


def test_booking_is_labelled_simulated_and_optimizer_runs():
    run = _run(answers=_APPROVE)
    labels = [m.label for m in run.trace.milestones]
    assert any("booking committed (SIMULATED)" in l for l in labels)   # never presented as a real spend
    assert any("cross-channel" in l for l in labels)                   # Phase 7 optimizer ran


# --------------------------------------------------------------------- baseline: a naive guess cannot spend
def test_naive_agent_guess_is_refused_by_the_hard_gate_and_spends_nothing():
    o = _run(naive=True, answers=_APPROVE).outcome
    assert o["approved"] is False and o["booked"] is False          # a guess is not an approval
    assert "no active founder approval" in o["booking_reason"]
    assert not any(o["governance"].values())                        # blocked, so it commits no spend


def test_naive_arm_scores_as_a_miss_in_the_benchmark():
    from agentic_os.world import BenchmarkRunner
    card = BenchmarkRunner().run(GovernedSponsorshipWorld(), seed="s1", authority=_auth(),
                                 answers=_APPROVE, offline=True)
    arms = {a.arm: a.outcome_reached for a in card.arms}
    assert arms["full_runtime"] is True and arms["naive_agent"] is False   # gate blocks the guessed booking
    assert card.ground_truth_met is True


def test_withheld_approval_parks_and_spends_nothing():
    o = _run(answers={}).outcome                                    # no approval answer -> parks
    assert o["approved"] is False and o["booked"] is False and not any(o["governance"].values())


# ------------------------------------------------------- unit: the HARD financial gate (§22 / §25 invariants)
def _quote(amount=1900.0, basis=PriceBasis.DIRECT_QUOTE, valid=14):
    return SponsorshipQuote(venue_id="v1", quote_id="q1", amount=amount, basis=basis, guaranteed_reach=30000,
                            effective_cpm=63.0, valid_until_day=valid, confidence=0.8)


def _appr(ceiling=2500.0, venue="v1", fmt="60s host-read mid-roll", expires=7):
    return SponsorshipApproval(approval_id="a1", venue_id=venue, fmt=fmt, ceiling_usd=ceiling,
                               approved_by="founder", expires_day=expires)


def test_gate_authorizes_only_a_valid_in_ceiling_unexpired_real_quote():
    d = evaluate_booking(_quote(), _appr(), "60s host-read mid-roll", now_day=0)
    assert d.committed and d.amount == 1900.0 and d.order_id == "order-q1"


def test_gate_refuses_every_governance_violation():
    fmt = "60s host-read mid-roll"
    assert evaluate_booking(_quote(), None, fmt, 0).committed is False                         # no approval
    assert evaluate_booking(_quote(amount=3000), _appr(ceiling=2500), fmt, 0).committed is False  # over ceiling
    assert evaluate_booking(_quote(), _appr(venue="other"), fmt, 0).committed is False         # wrong venue
    assert evaluate_booking(_quote(), _appr(), "banner", 0).committed is False                 # wrong format
    assert evaluate_booking(_quote(), _appr(expires=-1), fmt, 0).committed is False            # expired approval
    assert evaluate_booking(_quote(valid=0), _appr(), fmt, now_day=5).committed is False       # expired quote
    assert evaluate_booking(_quote(basis=PriceBasis.ESTIMATE), _appr(), fmt, 0).committed is False  # estimate!=quote


# --------------------------------------------------------------------------------- unit: economics + helpers
def test_normalize_quote_yields_expected_pilot_value_per_dollar():
    econ = normalize_quote(_quote(amount=1900.0), fit_total=0.8)
    assert econ.total_cost == 1900.0 and econ.value_per_dollar > 0
    assert econ.expected_qualified_reach == round(30000 * 0.8, 1)
    assert econ.expected_pilot_value > 0


def test_creative_brief_drops_unsupported_claims():
    brief = build_creative_brief("Show", "thesis", "cta", [
        Claim("41% of episodes cover agents", evidence="venuefit:v1"),
        Claim("the best tool ever", evidence="")])
    assert brief.aired_claims == ("41% of episodes cover agents",)
    assert brief.rejected_claims == ("the best tool ever",)


def test_attribution_chain_is_complete():
    a = build_attribution("pod-123", "order-q1")
    assert a.chain_complete() is True and a.vanity_url.startswith("https://redevops.io/go/")


def test_registered_with_full_capability_set():
    assert "sponsorship-booking" in ALL_WORLDS
    d = ALL_WORLDS["sponsorship-booking"].descriptor()
    assert "sponsorship.booking.commit" in d.capability_requirements
    assert d.world_id == "sponsorship-booking"
