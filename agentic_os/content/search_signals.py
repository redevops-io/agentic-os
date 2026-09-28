"""Search-Intelligence signals (Content / Growth Intelligence plan §3, §8, §9).

Deterministic detectors over Google Search Console observations that surface where a site can win — the §3
signal categories — as `SearchSignal`s the Content Agent can act on ONE change at a time (§10). Mirrors the
revenue leakage detectors: pure `Optional[SearchSignal]` functions, evidence-first, thresholded so tiny
samples never overreact (§9: "position alone must never trigger an intervention"). Emergent intent is a
WATCH, not an immediate rewrite (§3.3); the rest PROPOSE once the observation window is met.

Semantic query↔page relevance is an input on the observation (0..1) — computing it needs embeddings, out of
scope here; a later slice supplies it. A GSC client feeding these observations is the sensor slice.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, List, Optional, Tuple


class SignalType(str, Enum):
    EXISTING_INTENT = "EXISTING_INTENT"
    NEAR_WIN = "NEAR_WIN"
    EMERGENT_INTENT = "EMERGENT_INTENT"
    MISSING_PAGE = "MISSING_PAGE"
    CANNIBALIZATION = "CANNIBALIZATION"
    CTR_OPPORTUNITY = "CTR_OPPORTUNITY"
    TOPIC_EXPANSION = "TOPIC_EXPANSION"


@dataclass(frozen=True)
class SearchObservation:
    """A GSC query→page reading, aggregated over an observation window."""
    query: str
    page_url: str
    impressions: int = 0
    clicks: int = 0
    ctr: float = 0.0                 # clicks / impressions (0..1)
    position: float = 0.0            # average position (1 = top)
    relevance: float = 0.0           # semantic query↔page fit, 0..1 (supplied upstream)
    days_observed: int = 0
    site_id: str = ""


@dataclass(frozen=True)
class Thresholds:
    min_impressions: int = 50            # below this the sample is too small to act on
    min_observation_days: int = 14       # PROPOSE only once observed this long, else WATCH
    min_relevance: float = 0.6
    near_win_pos_min: float = 5.0
    near_win_pos_max: float = 20.0
    ctr_max_position: float = 10.0       # CTR opportunity only where the ranking is already reasonable
    ctr_gap: float = 0.4                 # actual CTR must be this fraction under the position baseline
    emergent_min_relevance: float = 0.9  # a new pairing needs very high fit to be worth watching
    emergent_min_impressions: int = 10


@dataclass(frozen=True)
class SearchSignal:
    signal_type: SignalType
    query: str
    affected_pages: Tuple[str, ...]
    confidence: float
    status: str                          # WATCH (observe more) | PROPOSE (evidence threshold met)
    proposed_action: str
    evidence: Tuple[str, ...] = field(default_factory=tuple)
    site_id: str = ""


# position → a rough baseline CTR (organic SERP), for the CTR-opportunity comparison
_CTR_CURVE = {1: 0.28, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05, 6: 0.04, 7: 0.03, 8: 0.03, 9: 0.025, 10: 0.02}
_NONCANONICAL = re.compile(r"/(blog|posts?|news|20\d\d)/", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _expected_ctr(position: float) -> float:
    return _CTR_CURVE.get(int(round(position)), 0.015)


def _status(o: SearchObservation, th: Thresholds) -> str:
    return "PROPOSE" if o.days_observed >= th.min_observation_days else "WATCH"


def near_win(o: SearchObservation, *, th: Thresholds = Thresholds()) -> Optional[SearchSignal]:
    """§3.2: a page ranking ~5–20 with enough impressions and strong query fit — one push could win it.
    Position alone never triggers this (§9): impressions + relevance are required."""
    if o.impressions < th.min_impressions or o.relevance < th.min_relevance:
        return None
    if not (th.near_win_pos_min <= o.position <= th.near_win_pos_max):
        return None
    conf = round(min(0.9, 0.4 + 0.3 * o.relevance + min(0.2, o.impressions / (5 * th.min_impressions))), 3)
    return SearchSignal(
        SignalType.NEAR_WIN, o.query, (o.page_url,), conf, _status(o, th),
        proposed_action="Strengthen the ranking page for this near-win query "
                        "(exact-query section / title / FAQ / internal links)",
        evidence=(f"position {o.position:.1f} in near-win band, {o.impressions} impressions, "
                  f"relevance {o.relevance:.2f}",), site_id=o.site_id)


def emergent_intent(current: SearchObservation, *, baseline_impressions: int,
                    th: Thresholds = Thresholds()) -> Optional[SearchSignal]:
    """§3.3: Google recently started testing a page for a query that was absent/negligible before. Low
    sample + very high fit → WATCH (record, don't rewrite yet); promote only when the threshold is met."""
    if current.relevance < th.emergent_min_relevance:
        return None
    if baseline_impressions >= th.min_impressions:
        return None                                   # already present → not emergent
    if current.impressions < th.emergent_min_impressions:
        return None                                   # need at least some signal
    return SearchSignal(
        SignalType.EMERGENT_INTENT, current.query, (current.page_url,),
        round(min(0.8, 0.4 + 0.4 * current.relevance), 3), "WATCH",
        proposed_action="Watch — Google is testing this page for a new query; promote to an intervention "
                        "only when the evidence threshold is met",
        evidence=(f"new query/page pairing, {current.impressions} impressions "
                  f"(baseline {baseline_impressions}), relevance {current.relevance:.2f}",), site_id=current.site_id)


def missing_page(o: SearchObservation, *, canonical_pages: Iterable[str] = (),
                 th: Thresholds = Thresholds()) -> Optional[SearchSignal]:
    """§3.4: a strong-fit intent ranks on a related but NON-canonical page (e.g. a blog post) — it deserves
    its own canonical page. Skip if a canonical page is already the one ranking."""
    if o.impressions < th.min_impressions or o.relevance < th.min_relevance:
        return None
    if o.page_url in set(canonical_pages) or not _NONCANONICAL.search(o.page_url):
        return None
    return SearchSignal(
        SignalType.MISSING_PAGE, o.query, (o.page_url,),
        round(min(0.9, 0.5 + 0.4 * o.relevance), 3), _status(o, th),
        proposed_action=f"CREATE_CANONICAL_PAGE for '{o.query}' — currently ranking a non-canonical page; "
                        f"link the existing page + related pages to it",
        evidence=(f"non-canonical page ranks for a high-fit intent ({o.impressions} impressions, "
                  f"relevance {o.relevance:.2f})",), site_id=o.site_id)


def ctr_opportunity(o: SearchObservation, *, th: Thresholds = Thresholds()) -> Optional[SearchSignal]:
    """§3.6: a reasonable ranking with meaningful impressions but CTR materially under the position baseline —
    a title/description/SERP-framing rewrite, not a content rewrite."""
    if o.impressions < th.min_impressions or o.position > th.ctr_max_position:
        return None
    expected = _expected_ctr(o.position)
    if o.ctr >= expected * (1.0 - th.ctr_gap):
        return None
    return SearchSignal(
        SignalType.CTR_OPPORTUNITY, o.query, (o.page_url,),
        round(min(0.85, 0.5 + (expected - o.ctr) / max(1e-6, expected) * 0.35), 3), _status(o, th),
        proposed_action="Rewrite title / meta description / SERP framing (ranking is fine, CTR is low)",
        evidence=(f"CTR {o.ctr:.1%} vs ~{expected:.1%} baseline at position {o.position:.1f}, "
                  f"{o.impressions} impressions",), site_id=o.site_id)


def cannibalization(observations: Iterable[SearchObservation], *,
                    th: Thresholds = Thresholds()) -> List[SearchSignal]:
    """§3.5: two or more pages repeatedly receive impressions for the same intent (query). Consolidate /
    canonicalize / differentiate."""
    by_query = defaultdict(list)
    for o in observations:
        if o.impressions >= th.min_impressions:
            by_query[_norm(o.query)].append(o)
    out: List[SearchSignal] = []
    for obs in by_query.values():
        pages = sorted({o.page_url for o in obs})
        if len(pages) >= 2:
            out.append(SearchSignal(
                SignalType.CANNIBALIZATION, obs[0].query, tuple(pages),
                round(min(0.85, 0.5 + 0.1 * len(pages)), 3), "PROPOSE",
                proposed_action="Resolve cannibalization — consolidate / canonicalize / differentiate "
                                "titles + internal links across the competing pages",
                evidence=(f"{len(pages)} pages competing for the same intent", *pages), site_id=obs[0].site_id))
    return out
