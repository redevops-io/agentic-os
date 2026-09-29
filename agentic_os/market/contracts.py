"""Canonical Market / Acquisition Intelligence evidence (Market-Intelligence plan §4–§9, Phase 0).

The observable-market evidence layer for the Content/Search Intelligence loop: observe how the market acquires
customers (competitor landing pages, funnels, creatives), reconstruct the funnels behind those observations,
extract recurring patterns, and match them to first-party gaps as candidate experiments. These are the typed,
provider-neutral, evidence-preserving objects that flow through:

    observe → resolve → detect → hypothesize → propose → approve → execute → measure → learn

Two principles the schema encodes:
  * **Weak evidence, honestly labelled.** Competitor creative volume / persistence is a *proxy*, not proof —
    a :class:`MarketPattern` carries both supporting AND contradicting evidence and a confidence, and a
    media field distinguishes *observed* text from *derived* inference. First-party measured outcomes
    (Revenue / Umami / GSC) supersede imitation; that join happens later, over these objects.
  * **Observation is READ-ONLY.** Nothing here publishes, spends, or changes a site — capturing a competitor
    page is a sensor. Consequential experiments are separately governed by the Mission Runtime.

Reuses the Integration-Plane :class:`BusinessObject` / :class:`Provenance` base (digest, bitemporal
observed_at/known_at, evidence refs) so market evidence is content-addressable and replayable like every other
canonical object; raw payloads (HTML, screenshots, video) stay as evidence artifacts referenced by
``prov.evidence_refs``, never inlined.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import ClassVar, Dict, Tuple

from ..integrations.business.contracts import BusinessObject, Provenance, now_ms

__all__ = [
    "Provenance", "TrackedCompany", "PageSnapshot", "MediaArtifact", "MediaAnalysis",
    "CTA", "Offer", "FormObservation", "FunnelStep", "Funnel", "MarketPattern", "Opportunity",
    "MarketObservations",
]


# ── who / what we observe ─────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TrackedCompany(BusinessObject):
    """A company whose public acquisition activity we observe. `role` keeps the corpus honest about why it's
    tracked (a direct competitor vs an adjacent reference)."""
    KIND: ClassVar[str] = "market.company"
    name: str = ""
    domain: str = ""
    category: str = ""
    role: str = "competitor"           # competitor | adjacent | reference


@dataclass(frozen=True)
class PageSnapshot(BusinessObject):
    """A public page observed at a point in time. `content_hash` drives change detection; the raw HTML /
    screenshot lives in `prov.evidence_refs`."""
    KIND: ClassVar[str] = "market.page_snapshot"
    company_ref: str = ""
    url: str = ""
    page_kind: str = "unknown"         # home | landing | pricing | form | demo | blog | unknown
    title: str = ""
    content_hash: str = ""
    captured_at: str = ""              # ISO datetime


@dataclass(frozen=True)
class MediaArtifact(BusinessObject):
    """A creative asset (image/video) observed on a page or ad. First-class evidence — the source frame/asset
    is retained via `prov.evidence_refs`."""
    KIND: ClassVar[str] = "market.media"
    company_ref: str = ""
    url: str = ""
    media_kind: str = "image"          # image | video
    source_page: str = ""


@dataclass(frozen=True)
class MediaAnalysis(BusinessObject):
    """Vision/audio extraction from a MediaArtifact. `observed` marks fields lifted directly from the media
    (on-screen text, transcript) vs `derived` inference — never blur the two (plan §7, §25)."""
    KIND: ClassVar[str] = "market.media_analysis"
    media_ref: str = ""
    on_screen_text: str = ""
    transcript: str = ""
    hooks: Tuple[str, ...] = ()
    cta_structure: Tuple[str, ...] = ()
    observed: bool = True


# ── funnel elements extracted from a page ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CTA(BusinessObject):
    KIND: ClassVar[str] = "market.cta"
    page_ref: str = ""
    text: str = ""
    action: str = ""                   # signup | demo | trial | contact | download | book | subscribe


@dataclass(frozen=True)
class Offer(BusinessObject):
    KIND: ClassVar[str] = "market.offer"
    page_ref: str = ""
    description: str = ""
    price: str = ""
    kind: str = ""                     # trial | freemium | discount | lead_magnet | bundle | demo


@dataclass(frozen=True)
class FormObservation(BusinessObject):
    KIND: ClassVar[str] = "market.form"
    page_ref: str = ""
    fields: Tuple[str, ...] = ()
    purpose: str = ""                  # signup | quiz | calculator | demo_request | newsletter | contact


# ── reconstructed funnel ──────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FunnelStep(BusinessObject):
    """One stage of a reconstructed acquisition funnel. `ref` points at the snapshot/media/cta/offer backing
    it; `attaches_to` is the previous step's provider_ref ("" for the entry)."""
    KIND: ClassVar[str] = "market.funnel_step"
    stage: str = ""                    # creative | landing | quiz | form | offer | signup | followup
    url: str = ""
    ref: str = ""
    attaches_to: str = ""


@dataclass(frozen=True)
class Funnel(BusinessObject):
    """A company's reconstructed public acquisition path (plan §3): creative/ad → landing → quiz/form/
    calculator/demo → offer/pricing/trial → signup → observable follow-up."""
    KIND: ClassVar[str] = "market.funnel"
    company_ref: str = ""
    entry_url: str = ""
    steps: Tuple[FunnelStep, ...] = ()

    def stages(self) -> Tuple[str, ...]:
        return tuple(s.stage for s in self.steps)


# ── patterns + opportunities (weak evidence, honestly scored) ─────────────────────────────────────────────
@dataclass(frozen=True)
class MarketPattern(BusinessObject):
    """A recurring acquisition pattern seen across competitors — NOT proof anything works. Carries both
    supporting and contradicting evidence + a confidence; `company_refs` is who exhibits it."""
    KIND: ClassVar[str] = "market.pattern"
    kind: str = ""                     # cta_pattern | offer_pattern | funnel_mechanic | strategy_shift | lead_magnet
    description: str = ""
    company_refs: Tuple[str, ...] = ()
    supporting_refs: Tuple[str, ...] = ()
    contradicting_refs: Tuple[str, ...] = ()
    confidence: float = 0.0


@dataclass(frozen=True)
class Opportunity(BusinessObject):
    """A candidate experiment: a pattern matched to a first-party gap on one of our sites. Feeds experiment
    planning (Context Runtime) → approval → Mission. `reversibility` + `expected_value` inform the gate;
    nothing here executes."""
    KIND: ClassVar[str] = "market.opportunity"
    pattern_ref: str = ""
    site: str = ""
    gap: str = ""
    proposed_experiment: str = ""
    expected_value: float = 0.0
    reversibility: str = "reversible"  # reversible | hard_to_reverse
    evidence_refs: Tuple[str, ...] = ()
    confidence: float = 0.0


# ── the observation bundle (one adapter's output, mergeable across sources) ────────────────────────────────
@dataclass(frozen=True)
class MarketObservations:
    """A provider-neutral bundle of market evidence — the unit a :class:`MarketSourceAdapter` returns and the
    :class:`FunnelResolver` / pattern detection consume. Mergeable so several sources compose into one view."""
    companies: Tuple[TrackedCompany, ...] = ()
    snapshots: Tuple[PageSnapshot, ...] = ()
    media: Tuple[MediaArtifact, ...] = ()
    ctas: Tuple[CTA, ...] = ()
    offers: Tuple[Offer, ...] = ()
    forms: Tuple[FormObservation, ...] = ()
    funnels: Tuple[Funnel, ...] = ()

    def merge(self, other: "MarketObservations") -> "MarketObservations":
        return MarketObservations(**{
            f.name: tuple(getattr(self, f.name)) + tuple(getattr(other, f.name)) for f in fields(self)})

    def counts(self) -> Dict[str, int]:
        return {f.name: n for f in fields(self) if (n := len(getattr(self, f.name)))}

    def is_empty(self) -> bool:
        return not any(getattr(self, f.name) for f in fields(self))

    def for_company(self, company_ref: str) -> "MarketObservations":
        """The subset observed for one company (by its provenance ref) — what the FunnelResolver works on."""
        def keep(objs, attr="company_ref"):
            return tuple(o for o in objs if getattr(o, attr, "") == company_ref)
        pages = keep(self.snapshots)
        page_refs = {p.prov.provider_ref for p in pages}
        return MarketObservations(
            companies=tuple(c for c in self.companies if c.prov.provider_ref == company_ref),
            snapshots=pages, media=keep(self.media),
            ctas=tuple(c for c in self.ctas if c.page_ref in page_refs),
            offers=tuple(o for o in self.offers if o.page_ref in page_refs),
            forms=tuple(fm for fm in self.forms if fm.page_ref in page_refs),
            funnels=keep(self.funnels))
