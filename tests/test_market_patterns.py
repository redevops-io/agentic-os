"""Cross-competitor pattern detection (Market-Intelligence plan §9, Phase 3).

Two competitors both run a free trial; only one runs a demo CTA. Asserts prevalence-based confidence and
contradicting evidence, the min_companies threshold, and creative-hook patterns from observed media analyses.

Run:  uv run python -m pytest tests/test_market_patterns.py -q
"""
from __future__ import annotations

from agentic_os.market import MarketObservations, detect_patterns
from agentic_os.market.contracts import (
    CTA, MediaAnalysis, MediaArtifact, Offer, PageSnapshot, Provenance, TrackedCompany)


def _p(ref):
    return Provenance("website", ref)


def _snap(co):
    return PageSnapshot(prov=_p(f"{co}:/"), company_ref=co, url=f"https://{co}/", page_kind="home")


def _obs():
    return MarketObservations(
        companies=(TrackedCompany(prov=_p("acme"), name="Acme"),
                   TrackedCompany(prov=_p("globex"), name="Globex"),
                   TrackedCompany(prov=_p("initech"), name="Initech")),
        snapshots=(_snap("acme"), _snap("globex"), _snap("initech")),
        offers=(  # trial at acme + globex (2/3); freemium only at initech (1/3)
            Offer(prov=_p("acme:offer:trial"), page_ref="acme:/", kind="trial"),
            Offer(prov=_p("globex:offer:trial"), page_ref="globex:/", kind="trial"),
            Offer(prov=_p("initech:offer:freemium"), page_ref="initech:/", kind="freemium"),
        ),
        ctas=(  # demo CTA only at acme (1/3 → below min_companies=2)
            CTA(prov=_p("acme:cta1"), page_ref="acme:/", action="demo"),
            CTA(prov=_p("acme:cta2"), page_ref="acme:/", action="signup"),
            CTA(prov=_p("globex:cta1"), page_ref="globex:/", action="signup"),
        ),
    )


def test_offer_pattern_prevalence_and_contradiction():
    pats = detect_patterns(_obs())
    trial = next(p for p in pats if p.kind == "offer_pattern" and "trial" in p.description)
    assert set(trial.company_refs) == {"acme", "globex"}
    assert round(trial.confidence, 2) == 0.67                # 2 of 3
    assert trial.contradicting_refs == ("initech",)          # the company without it = contradicting evidence


def test_min_companies_threshold_filters_singletons():
    pats = detect_patterns(_obs())
    # freemium (1 company) and demo CTA (1 company) are below min_companies=2 → not patterns
    assert not any("freemium" in p.description for p in pats)
    assert not any(p.kind == "cta_pattern" and "demo" in p.description for p in pats)
    # signup CTA (acme+globex) IS a pattern
    assert any(p.kind == "cta_pattern" and "signup" in p.description for p in pats)


def test_creative_hooks_from_observed_analyses():
    obs = MarketObservations(
        companies=(TrackedCompany(prov=_p("acme"), name="Acme"),
                   TrackedCompany(prov=_p("globex"), name="Globex")),
        media=(MediaArtifact(prov=_p("acme:m1"), company_ref="acme", media_kind="image"),
               MediaArtifact(prov=_p("globex:m1"), company_ref="globex", media_kind="image")))
    analyses = (
        MediaAnalysis(prov=_p("acme:m1#a"), media_ref="acme:m1", hooks=("Ship in minutes",), observed=True),
        MediaAnalysis(prov=_p("globex:m1#a"), media_ref="globex:m1", hooks=("ship in minutes",), observed=True),
        MediaAnalysis(prov=_p("x#a"), media_ref="acme:m1", hooks=("ignored",), observed=False),  # unobserved
    )
    pats = detect_patterns(obs, analyses=analyses)
    hook = next(p for p in pats if p.kind == "creative_hook")
    assert set(hook.company_refs) == {"acme", "globex"} and hook.confidence == 1.0  # normalized, both cos


def test_sorted_by_confidence_desc():
    pats = detect_patterns(_obs())
    assert pats == tuple(sorted(pats, key=lambda p: p.confidence, reverse=True))
