"""Cross-competitor pattern detection (Market-Intelligence plan §9, Phase 3).

Turn a bundle of observations (+ optional media analyses) into :class:`MarketPattern` objects: which
offers/CTAs/funnel mechanics/creative hooks recur *across* competitors. Deterministic and honest about weak
evidence — every pattern's confidence is the **prevalence** (fraction of tracked companies exhibiting it) and
it carries the companies that do NOT exhibit it as ``contradicting_refs``. A pattern is never a claim that the
tactic works; it's a candidate to test, and first-party outcomes supersede it downstream (plan §25).

Pure functions over the canonical contracts — no network, no model.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Sequence, Set, Tuple

from .contracts import MarketObservations, MarketPattern, MediaAnalysis, Provenance


def _companies(obs: MarketObservations) -> Set[str]:
    return ({c.prov.provider_ref for c in obs.companies}
            or {s.company_ref for s in obs.snapshots})


def _page_to_company(obs: MarketObservations) -> Dict[str, str]:
    return {s.prov.provider_ref: s.company_ref for s in obs.snapshots}


def _media_to_company(obs: MarketObservations) -> Dict[str, str]:
    return {m.prov.provider_ref: m.company_ref for m in obs.media}


def _pattern(kind: str, key: str, by_company: Dict[str, List[str]], all_companies: Set[str],
             total: int) -> MarketPattern:
    exhibiting = sorted(by_company)
    supporting = tuple(r for refs in by_company.values() for r in refs)
    contradicting = tuple(sorted(all_companies - set(exhibiting)))
    return MarketPattern(
        prov=Provenance("internal.pattern_detector", f"{kind}:{key}"),
        kind=kind, description=f"{key} — seen at {len(exhibiting)}/{total} tracked companies",
        company_refs=tuple(exhibiting), supporting_refs=supporting,
        contradicting_refs=contradicting, confidence=round(len(exhibiting) / total, 3))


def detect_patterns(obs: MarketObservations, analyses: Sequence[MediaAnalysis] = (), *,
                    min_companies: int = 2) -> Tuple[MarketPattern, ...]:
    """Detect offer / CTA / funnel-mechanic / creative-hook patterns recurring across >= `min_companies`
    tracked companies. Confidence = prevalence; companies without the pattern are its contradicting evidence."""
    companies = _companies(obs)
    total = len(companies) or 1
    page_co = _page_to_company(obs)
    media_co = _media_to_company(obs)
    out: List[MarketPattern] = []

    def emit(kind: str, groups: Dict[str, Dict[str, List[str]]]) -> None:
        for key, by_company in groups.items():
            if len(by_company) >= min_companies:
                out.append(_pattern(kind, key, by_company, companies, total))

    # offers by kind
    offer_groups: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for o in obs.offers:
        co = page_co.get(o.page_ref, "")
        if co:
            offer_groups[o.kind][co].append(o.prov.provider_ref)
    emit("offer_pattern", offer_groups)

    # CTAs by action
    cta_groups: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for c in obs.ctas:
        co = page_co.get(c.page_ref, "")
        if co and c.action:
            cta_groups[c.action][co].append(c.prov.provider_ref)
    emit("cta_pattern", cta_groups)

    # funnel mechanics — lead-magnet forms (quiz/calculator) across competitors
    mech_groups: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for fm in obs.forms:
        co = page_co.get(fm.page_ref, "")
        if co and fm.purpose in ("quiz", "calculator", "demo_request"):
            mech_groups[fm.purpose][co].append(fm.prov.provider_ref)
    emit("funnel_mechanic", mech_groups)

    # creative hooks from media analyses (observed only) — normalized hook text across competitors
    hook_groups: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for a in analyses:
        if not a.observed:
            continue
        co = media_co.get(a.media_ref, "")
        if not co:
            continue
        for hook in a.hooks:
            hook_groups[hook.strip().lower()][co].append(a.prov.provider_ref)
    emit("creative_hook", hook_groups)

    # most prevalent first
    out.sort(key=lambda p: p.confidence, reverse=True)
    return tuple(out)
