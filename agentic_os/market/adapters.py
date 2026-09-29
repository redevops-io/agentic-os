"""Market-Intelligence seams (Market-Intelligence plan §4, §6, §7, Phase 0).

Three protocols the rest of the program composes, plus a registry and a deterministic reference funnel
resolver so the schema is proven end-to-end:

  * :class:`MarketSourceAdapter` — a READ-ONLY sensor over one market source (a competitor's site, a public
    creative/ad source). Discovers pages/media for a tracked company and returns canonical
    :class:`MarketObservations`. Self-skips (unreachable → empty). Credentialed / competitor-specific
    implementations live in the private overlay; the contract is public.
  * :class:`MediaAnalyzer` — vision/audio extraction from a :class:`MediaArtifact` → :class:`MediaAnalysis`
    (a real one wraps a multimodal model; observed vs derived is preserved).
  * :class:`FunnelResolver` — reconstruct a company's acquisition :class:`Funnel` from its observations.

No third-party provider defines the canonical schema (plan §24): adapters normalize *into* these contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Protocol, Tuple, runtime_checkable

from .contracts import (
    CTA, Funnel, FunnelStep, MarketObservations, MediaAnalysis, MediaArtifact, Offer, PageSnapshot,
    TrackedCompany)


@runtime_checkable
class MarketSourceAdapter(Protocol):
    """A read-only market observation source. ``provider`` is its stable id (matches the provenance provider
    on the objects it emits); ``connected`` is a cheap self-skip probe; ``observe`` pulls current public
    evidence for one tracked company."""
    provider: str

    def connected(self) -> bool: ...

    def observe(self, company: TrackedCompany) -> MarketObservations: ...


@runtime_checkable
class MediaAnalyzer(Protocol):
    def analyze(self, media: MediaArtifact) -> MediaAnalysis: ...


@runtime_checkable
class FunnelResolver(Protocol):
    def reconstruct(self, company_ref: str, obs: MarketObservations) -> Funnel: ...


@dataclass
class MarketSourceRegistry:
    """The market sources a deployment has wired; ``observe_all`` merges every connected source's view of a
    company into one :class:`MarketObservations`."""
    _sources: Dict[str, MarketSourceAdapter] = field(default_factory=dict)

    def register(self, source: MarketSourceAdapter) -> MarketSourceAdapter:
        self._sources[source.provider] = source
        return source

    def connected(self) -> Tuple[MarketSourceAdapter, ...]:
        return tuple(s for s in self._sources.values() if s.connected())

    def observe(self, company: TrackedCompany) -> MarketObservations:
        obs = MarketObservations(companies=(company,))
        for s in self.connected():
            obs = obs.merge(s.observe(company))
        return obs


# ── deterministic reference funnel resolver ───────────────────────────────────────────────────────────────
# Canonical acquisition path (plan §3): creative → landing → quiz/form/calculator/demo → offer → signup.
_STAGE_ORDER = ("creative", "landing", "quiz", "form", "offer", "signup", "followup")
_PAGE_STAGE = {"home": "landing", "landing": "landing", "pricing": "offer", "demo": "form", "form": "form"}
_FORM_STAGE = {"quiz": "quiz", "calculator": "quiz", "demo_request": "form", "signup": "signup",
               "newsletter": "followup", "contact": "form"}
_SIGNUP_ACTIONS = {"signup", "trial", "subscribe", "book"}


@dataclass
class SimpleFunnelResolver:
    """A deterministic funnel reconstruction from observed pages/media/forms/offers/CTAs — no model needed.
    It orders the observed elements into the canonical stages and chains them; a real resolver can refine
    ordering with page-graph link analysis later. Pure + leakage-free (works only from the passed evidence)."""

    def reconstruct(self, company_ref: str, obs: MarketObservations) -> Funnel:
        sub = obs.for_company(company_ref)
        staged: List[FunnelStep] = []

        def add(stage: str, url: str, ref: str) -> None:
            staged.append(FunnelStep(prov=_ref_prov(ref), stage=stage, url=url, ref=ref))

        if sub.media:
            m = sub.media[0]
            add("creative", m.url, m.prov.provider_ref)
        entry_url = ""
        for pg in sub.snapshots:
            stage = _PAGE_STAGE.get(pg.page_kind)
            if stage:
                add(stage, pg.url, pg.prov.provider_ref)
                if stage == "landing" and not entry_url:
                    entry_url = pg.url
        for fm in sub.forms:
            add(_FORM_STAGE.get(fm.purpose, "form"), "", fm.prov.provider_ref)
        for of in sub.offers:
            add("offer", "", of.prov.provider_ref)
        if any(c.action in _SIGNUP_ACTIONS for c in sub.ctas):
            c = next(c for c in sub.ctas if c.action in _SIGNUP_ACTIONS)
            add("signup", "", c.prov.provider_ref)

        # de-dupe stages keeping first, order canonically, chain attaches_to
        seen: set = set()
        ordered: List[FunnelStep] = []
        for stage in _STAGE_ORDER:
            for st in staged:
                if st.stage == stage and stage not in seen:
                    seen.add(stage)
                    ordered.append(st)
        chained: List[FunnelStep] = []
        prev = ""
        for st in ordered:
            chained.append(FunnelStep(prov=st.prov, stage=st.stage, url=st.url, ref=st.ref, attaches_to=prev))
            prev = st.ref
        entry_url = entry_url or (chained[0].url if chained else "")
        return Funnel(prov=_ref_prov(company_ref), company_ref=company_ref, entry_url=entry_url,
                      steps=tuple(chained))


def _ref_prov(ref: str):
    from .contracts import Provenance
    return Provenance(provider="internal.funnel_resolver", provider_ref=ref)
