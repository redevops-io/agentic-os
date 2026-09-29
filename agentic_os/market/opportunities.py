"""Pattern → first-party gap matching (Market-Intelligence plan §10–§11, Phase 4 core).

Compare detected :class:`MarketPattern`s against our OWN site's observed elements (obtained by pointing the
same :class:`WebsiteSourceAdapter` at our domain) and emit an :class:`Opportunity` wherever a prevalent
competitor pattern is ABSENT on our site. That's the honest hypothesis: "most tracked competitors do X, we
don't — worth testing." Expected value carries the pattern's prevalence; the opportunity is a candidate for
governed experiment planning (Phase 5), never an action.

Deterministic and pure. A pattern we already exhibit yields no opportunity. Creative-hook patterns need our own
creative analysis to compare, so they're out of scope here (they surface as patterns, not auto-opportunities).
"""
from __future__ import annotations

from typing import List, Tuple

from .contracts import MarketObservations, MarketPattern, Opportunity, Provenance

# how a pattern kind maps to "what would close the gap on our site", and where to read our current state.
_EXPERIMENT = {
    "offer_pattern": "Test a {key} offer on the relevant acquisition page",
    "cta_pattern": "Add/clarify a {key} call-to-action above the fold",
    "funnel_mechanic": "Add a {key} step (lead magnet) to the funnel",
}


def _key(pattern: MarketPattern) -> str:
    # provider_ref is "<kind>:<key>" (see patterns.detect_patterns)
    ref = pattern.prov.provider_ref
    return ref.split(":", 1)[1] if ":" in ref else ref


def _own_signature(kind: str, own: MarketObservations) -> set:
    if kind == "offer_pattern":
        return {o.kind for o in own.offers}
    if kind == "cta_pattern":
        return {c.action for c in own.ctas}
    if kind == "funnel_mechanic":
        return {f.purpose for f in own.forms}
    return set()


def match_opportunities(patterns: Tuple[MarketPattern, ...], own: MarketObservations, *, site: str,
                        min_confidence: float = 0.0) -> Tuple[Opportunity, ...]:
    """Emit an Opportunity for each pattern (of a gap-matchable kind) our site does not already exhibit,
    ranked by expected value (the pattern's prevalence). `own` is our own site observed with the same adapter."""
    out: List[Opportunity] = []
    for p in patterns:
        tmpl = _EXPERIMENT.get(p.kind)
        if tmpl is None or p.confidence < min_confidence:
            continue
        key = _key(p)
        if key in _own_signature(p.kind, own):
            continue                                   # we already do it — not a gap
        out.append(Opportunity(
            prov=Provenance("internal.opportunity_matcher", f"{site}:{p.prov.provider_ref}"),
            pattern_ref=p.prov.provider_ref, site=site,
            gap=f"{key} ({p.kind}) is absent on {site} but present at {len(p.company_refs)} tracked competitors",
            proposed_experiment=tmpl.format(key=key),
            expected_value=p.confidence, reversibility="reversible",
            evidence_refs=p.supporting_refs, confidence=p.confidence))
    out.sort(key=lambda o: o.expected_value, reverse=True)
    return tuple(out)
