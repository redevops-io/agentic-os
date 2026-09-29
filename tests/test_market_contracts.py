"""Market / Acquisition Intelligence contracts + seams (Market-Intelligence plan, Phase 0).

Proves the canonical evidence schema (digest/provenance/bitemporal via the shared BusinessObject base), the
observation bundle (merge / counts / for_company scoping), the protocol seams, and a deterministic funnel
reconstruction from observed pages/forms/offers/CTAs — offline, no network.

Run:  uv run python -m pytest tests/test_market_contracts.py -q
"""
from __future__ import annotations

from agentic_os.market import (
    CTA, FormObservation, Funnel, MarketObservations, MarketSourceAdapter, MarketSourceRegistry, MediaArtifact,
    Offer, Opportunity, PageSnapshot, SimpleFunnelResolver, TrackedCompany)
from agentic_os.market.contracts import MarketPattern, Provenance

CO = "acme"


def _p(ref):
    return Provenance(provider="webcapture", provider_ref=ref)


def _obs():
    return MarketObservations(
        companies=(TrackedCompany(prov=_p(CO), name="Acme", domain="acme.example", role="competitor"),),
        snapshots=(
            PageSnapshot(prov=_p("acme:home"), company_ref=CO, url="https://acme.example/", page_kind="home",
                         content_hash="h1"),
            PageSnapshot(prov=_p("acme:pricing"), company_ref=CO, url="https://acme.example/pricing",
                         page_kind="pricing", content_hash="h2"),
        ),
        media=(MediaArtifact(prov=_p("acme:vid1"), company_ref=CO, url="https://acme.example/ad.mp4",
                             media_kind="video"),),
        ctas=(CTA(prov=_p("acme:cta1"), page_ref="acme:home", text="Start free trial", action="trial"),),
        offers=(Offer(prov=_p("acme:offer1"), page_ref="acme:pricing", description="14-day trial", kind="trial"),),
        forms=(FormObservation(prov=_p("acme:form1"), page_ref="acme:home", fields=("email",), purpose="signup"),),
    )


# ── schema ────────────────────────────────────────────────────────────────────────────────────────────────
def test_objects_are_content_addressed_and_provenanced():
    a = PageSnapshot(prov=_p("x"), company_ref=CO, url="u", page_kind="landing", content_hash="h")
    b = PageSnapshot(prov=Provenance(provider="other", provider_ref="y"), company_ref=CO, url="u",
                     page_kind="landing", content_hash="h")
    assert a.digest() == b.digest()                 # digest excludes provenance → identical facts compare equal
    d = a.to_dict()
    assert d["kind"] == "market.page_snapshot" and d["prov"]["provider"] == "webcapture"


def test_pattern_carries_supporting_and_contradicting_evidence():
    p = MarketPattern(prov=_p("pat1"), kind="offer_pattern", description="free trial is near-universal",
                      company_refs=(CO, "globex"), supporting_refs=("acme:offer1",),
                      contradicting_refs=("initech:offer1",), confidence=0.6)
    assert p.confidence == 0.6 and p.contradicting_refs  # weak evidence, honestly scored


# ── bundle ────────────────────────────────────────────────────────────────────────────────────────────────
def test_merge_counts_and_for_company_scope():
    merged = _obs().merge(MarketObservations(snapshots=(
        PageSnapshot(prov=_p("globex:home"), company_ref="globex", url="https://globex/", page_kind="home"),)))
    assert merged.counts()["snapshots"] == 3
    sub = merged.for_company(CO)
    assert {s.company_ref for s in sub.snapshots} == {CO}       # scoped to acme
    assert sub.ctas and sub.offers                              # page-linked children kept
    assert MarketObservations().is_empty() and not merged.is_empty()


# ── seams / deterministic funnel reconstruction ───────────────────────────────────────────────────────────
def test_simple_funnel_reconstructs_canonical_stages():
    f = SimpleFunnelResolver().reconstruct(CO, _obs())
    assert isinstance(f, Funnel) and f.company_ref == CO
    stages = f.stages()
    # creative (video) → landing (home) → offer (pricing/offer) → signup (trial CTA), canonical order
    assert stages == ("creative", "landing", "offer", "signup")
    assert f.entry_url == "https://acme.example/"
    # chained: each step attaches to the previous step's ref
    assert f.steps[0].attaches_to == "" and f.steps[1].attaches_to == f.steps[0].ref


def test_registry_merges_connected_sources_and_skips_disconnected():
    class _Src:
        provider = "webcapture"
        def __init__(self, up): self.up = up
        def connected(self): return self.up
        def observe(self, company): return _obs()

    reg = MarketSourceRegistry()
    reg.register(_Src(up=False))                     # disconnected → skipped
    assert reg.observe(TrackedCompany(prov=_p(CO), name="Acme")).counts().get("snapshots", 0) == 0
    reg.register(_Src(up=True))
    assert reg.observe(TrackedCompany(prov=_p(CO), name="Acme")).counts()["snapshots"] == 2
    assert isinstance(_Src(True), MarketSourceAdapter)          # satisfies the protocol


def test_opportunity_defaults_to_reversible_and_scored():
    o = Opportunity(prov=_p("op1"), pattern_ref="pat1", site="redevops.io", gap="no trial CTA on pricing",
                    proposed_experiment="add a trial CTA above the fold", expected_value=0.3, confidence=0.5)
    assert o.reversibility == "reversible" and o.expected_value == 0.3
