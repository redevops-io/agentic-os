"""Pattern -> first-party gap matching (Market-Intelligence plan §10, Phase 4 core).

Competitors' patterns vs our own observed site: emit opportunities only for patterns we don't already exhibit,
ranked by prevalence.

Run:  uv run python -m pytest tests/test_market_opportunities.py -q
"""
from __future__ import annotations

from agentic_os.market import MarketObservations, detect_patterns, match_opportunities
from agentic_os.market.contracts import CTA, Offer, PageSnapshot, Provenance, TrackedCompany


def _p(ref):
    return Provenance("website", ref)


def _competitor_obs():
    def snap(co):
        return PageSnapshot(prov=_p(f"{co}:/"), company_ref=co, url=f"https://{co}/", page_kind="home")
    return MarketObservations(
        companies=(TrackedCompany(prov=_p("acme"), name="Acme"), TrackedCompany(prov=_p("globex"), name="Globex")),
        snapshots=(snap("acme"), snap("globex")),
        offers=(Offer(prov=_p("acme:offer:trial"), page_ref="acme:/", kind="trial"),
                Offer(prov=_p("globex:offer:trial"), page_ref="globex:/", kind="trial")),
        ctas=(CTA(prov=_p("acme:cta"), page_ref="acme:/", action="demo"),
              CTA(prov=_p("globex:cta"), page_ref="globex:/", action="demo")))


def _own(with_trial: bool):
    # our own site observed the same way; optionally already has a trial offer
    offers = (Offer(prov=_p("ours:offer:trial"), page_ref="ours:/", kind="trial"),) if with_trial else ()
    return MarketObservations(
        companies=(TrackedCompany(prov=_p("ours"), name="ReDevOps", domain="redevops.io"),),
        snapshots=(PageSnapshot(prov=_p("ours:/"), company_ref="ours", url="https://redevops.io/",
                                page_kind="home"),),
        offers=offers)


def test_gap_becomes_an_opportunity():
    pats = detect_patterns(_competitor_obs())
    opps = match_opportunities(pats, _own(with_trial=False), site="redevops.io")
    kinds = {o.pattern_ref for o in opps}
    assert "offer_pattern:trial" in kinds and "cta_pattern:demo" in kinds     # we have neither → both gaps
    trial = next(o for o in opps if o.pattern_ref == "offer_pattern:trial")
    assert trial.site == "redevops.io" and trial.expected_value == 1.0        # 2/2 competitors
    assert "trial" in trial.proposed_experiment and trial.reversibility == "reversible"
    assert trial.evidence_refs                                                # carries the supporting evidence


def test_already_exhibited_pattern_is_not_an_opportunity():
    pats = detect_patterns(_competitor_obs())
    opps = match_opportunities(pats, _own(with_trial=True), site="redevops.io")
    assert not any(o.pattern_ref == "offer_pattern:trial" for o in opps)      # we already run a trial
    assert any(o.pattern_ref == "cta_pattern:demo" for o in opps)             # still missing the demo CTA


def test_ranked_by_expected_value():
    opps = match_opportunities(detect_patterns(_competitor_obs()), _own(False), site="redevops.io")
    assert opps == tuple(sorted(opps, key=lambda o: o.expected_value, reverse=True))
