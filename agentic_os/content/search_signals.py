"""Search-Intelligence signals (Content / Growth Intelligence plan §3, §8, §9).

Deterministic detectors over Google Search Console observations that surface where a site can win — the §3
signal categories — as `SearchSignal`s the Content Agent can act on ONE change at a time (§10). Mirrors the
revenue leakage detectors: pure `Optional[SearchSignal]` functions, evidence-first, thresholded so tiny
samples never overreact (§9: "position alone must never trigger an intervention"). Emergent intent is a
WATCH, not an immediate rewrite (§3.3); the rest PROPOSE once the observation window is met.

Semantic query↔page relevance is an input on the observation (0..1) — computing it needs embeddings, out of
scope here; an optional, injected hook (`agentic_os.content.relevance`, wired through
`collect_search_observations(..., relevance_fn=...)`) can supply it. `relevance_source` records where the value
came from so "unknown" is distinguishable from "low" (`docs/SEARCH_RELEVANCE.md`); the detectors only act on
that distinction behind `AGENTIC_OS_RELEVANCE_UNKNOWN_AWARE` (default OFF). A GSC client feeding these
observations is the sensor slice.
"""
from __future__ import annotations

import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, List, Optional, Tuple

from agentic_os.content.relevance import sanitize_text


RELEVANCE_UNKNOWN_AWARE_ENV = "AGENTIC_OS_RELEVANCE_UNKNOWN_AWARE"
_TRUTHY = {"1", "true", "yes", "on"}
NOT_ASSESSABLE = "not assessable: relevance unknown"


def relevance_unknown_aware() -> bool:
    """Feature flag, default OFF. Off: a detector that needs relevance behaves exactly as before (a 0.0 / unset
    relevance suppresses it). On: it returns a WATCH 'not assessable' signal instead of staying silent."""
    return os.environ.get(RELEVANCE_UNKNOWN_AWARE_ENV, "").strip().lower() in _TRUTHY


class SignalType(str, Enum):
    EXISTING_INTENT = "EXISTING_INTENT"
    NEAR_WIN = "NEAR_WIN"
    EMERGENT_INTENT = "EMERGENT_INTENT"
    MISSING_PAGE = "MISSING_PAGE"
    CANNIBALIZATION = "CANNIBALIZATION"
    CTR_OPPORTUNITY = "CTR_OPPORTUNITY"
    TOPIC_EXPANSION = "TOPIC_EXPANSION"
    RELEVANCE_CONFLICT = "RELEVANCE_CONFLICT"     # WATCH only: differing relevance readings for one query/page


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
    # Where `relevance` came from. "unset" (default) = nobody supplied it, so 0.0 means UNKNOWN, not "irrelevant";
    # "unavailable:<reason>" = a relevance_fn tried and could not assess; "embedding:<name>@<revision>" = a
    # deterministic query↔page embedding similarity; any other label = a value supplied upstream (caller-owned).
    relevance_source: str = "unset"
    relevance_detail: str = ""       # provenance of an embedding value (embedder, map, text hashes); "" otherwise


def _source(o: object) -> str:
    """The observation's relevance source, normalised (stripped; None/empty -> 'unset'). Read with getattr so that
    duck-typed observation objects that predate the field keep working."""
    s = getattr(o, "relevance_source", None)
    s = "unset" if s is None else str(s).strip()
    return s or "unset"


def _detail(o: object) -> str:
    d = getattr(o, "relevance_detail", "")
    return "" if d is None else str(d).strip()


def _kind(src: str) -> str:
    """'unset' | 'unavailable' | 'embedding' | 'labelled' (case-insensitive on the reserved prefixes)."""
    low = src.lower()
    if low == "unset":
        return "unset"
    if low.startswith("unavailable:"):
        return "unavailable"
    if low.startswith("embedding:"):
        return "embedding"
    return "labelled"


def _value(o: object) -> Optional[float]:
    try:
        return float(getattr(o, "relevance", 0.0))
    except (TypeError, ValueError):
        return None


def relevance_state(o: SearchObservation, th: Optional["Thresholds"] = None) -> str:
    """'unknown' | 'invalid' | 'low' | 'ok'. The SOURCE is authoritative:
    * `unavailable:*` is always 'unknown' whatever number sits in `relevance` (it is only a placeholder);
    * a value that is NaN/inf/outside [0, 1] is 'invalid' under any source (treated as unknown by the
      unknown-aware detectors; flag-off numeric behaviour is untouched);
    * source 'unset' with the default 0.0 is 'unknown'; 'unset' with a NON-zero value is a supplied, unlabelled
      value and is judged on that value, exactly as before the source field existed;
    * everything else is measured: 'low' below `min_relevance`, else 'ok'. A measured 0.0 is 'low', never unknown."""
    th = th or Thresholds()
    kind = _kind(_source(o))
    if kind == "unavailable":
        return "unknown"
    v = _value(o)
    if v is None or not math.isfinite(v) or v < 0.0 or v > 1.0:
        return "invalid"
    if kind == "unset" and v == 0.0:
        return "unknown"
    return "low" if v < th.min_relevance else "ok"


def relevance_unknown(o: SearchObservation) -> bool:
    """True when relevance cannot be relied on: state 'unknown' or 'invalid'."""
    return relevance_state(o) in ("unknown", "invalid")


def hook_originated(o: SearchObservation) -> bool:
    """True when the source was written by the relevance hook (embedding value or explicit unavailable)."""
    return _kind(_source(o)) in ("embedding", "unavailable")


def relevance_reading(o: SearchObservation) -> Tuple[str, Optional[float], str]:
    """(source, value or None, provenance detail). The value is None whenever the relevance is UNKNOWN or
    invalid, so a placeholder 0.0 can never be read as a measured-low value."""
    state = relevance_state(o)
    return (_source(o), None if state in ("unknown", "invalid") else float(_value(o)), _detail(o))


def _join(*parts: str) -> str:
    return "; ".join(p for p in parts if p)


def _relevance_lines(o: SearchObservation, *, used: bool = True) -> Tuple[str, ...]:
    """Evidence stating the relevance SOURCE and embedder provenance. Observations that never touched the hook
    produce exactly the evidence they always did while the unknown-aware flag is off (bit-identical); an
    observation the hook wrote (embedding / unavailable) always states its source + provenance; with the flag on
    every signal states its relevance source, whichever detector produced it."""
    src = _source(o)
    kind = _kind(src)
    if kind == "embedding":
        return (_join(f"relevance provenance: {src}", _detail(o)),)
    if kind == "unavailable":
        return (_join(f"relevance unknown: relevance_source={src}", _detail(o)),)
    if relevance_unknown_aware():
        state = relevance_state(o)
        if state == "invalid":
            how = "invalid value, treated as unknown"
        elif kind == "unset":
            how = ("unknown, never measured" if state == "unknown"
                   else "supplied, unlabelled; label supplied values 'supplied:<label>'")
        else:
            how = "supplied upstream"
        return (f"relevance_source={src} ({how})" + ("" if used else "; not used by this detector"),)
    return ()


def conflict_key(o: SearchObservation) -> Tuple[str, str, str]:
    """Identity of one reading target: (site_id, page_url, the exact sanitised query text). Only readings of the
    SAME site, page and query can conflict; distinct queries (including all non-Latin ones) never do."""
    return (str(getattr(o, "site_id", "") or ""), str(getattr(o, "page_url", "") or ""),
            sanitize_text(str(getattr(o, "query", "") or ""), 10_000))


_ROUND = 9      # readings closer than 1e-9 are the same reading (float jitter is not a conflict)


def _conflict_groups(observations: Iterable[SearchObservation], require_hook: bool
                     ) -> List[Tuple[Tuple[str, str, str], "SearchSignal"]]:
    groups: dict = {}
    for o in observations:
        if _kind(_source(o)) == "unset":
            continue
        groups.setdefault(conflict_key(o), []).append(o)
    out: List[Tuple[Tuple[str, str, str], SearchSignal]] = []
    for key in sorted(groups):
        members = groups[key]
        if require_hook and not any(hook_originated(m) for m in members):
            continue
        readings = set()
        for m in members:
            src, val, detail = relevance_reading(m)
            readings.add((src, None if val is None else round(val, _ROUND), detail))
        ordered = sorted(readings, key=lambda r: (r[0], r[1] is None, 0.0 if r[1] is None else r[1], r[2]))
        measured = {r[1] for r in ordered if r[1] is not None}
        has_unknown = any(r[1] is None for r in ordered)
        kinds = []
        if len(measured) >= 2:
            kinds.append("measured values differ")
        if measured and has_unknown:
            kinds.append("measured and unknown readings mixed")
        if not kinds:
            continue
        lines = tuple(_join(f"reading: {src}", "unknown" if val is None else f"value={val:.3f}", detail)
                      for src, val, detail in ordered)
        out.append((key, SearchSignal(
            SignalType.RELEVANCE_CONFLICT, min(str(getattr(m, "query", "")) for m in members), (key[1],), 0.0,
            "WATCH",
            proposed_action="Relevance conflict: several readings disagree for this query/page; none was averaged "
                            "or chosen — resolve (same page text, one embedder) before acting on relevance",
            evidence=(f"relevance conflict: {'; '.join(kinds)} ({len(ordered)} distinct readings)",) + lines,
            site_id=key[0])))
    return out


def relevance_conflicts(observations: Iterable[SearchObservation], *, require_hook: bool = False
                        ) -> List["SearchSignal"]:
    """WATCH signals for a (site, page, query) read more than once with DIFFERING relevance: two measured values
    that differ by more than 1e-9 (same or different embedder), or a measured value next to an unknown one.
    Nothing is averaged, chosen or overwritten: every reading is listed in the evidence and each observation is
    still judged on its own reading. Identical repeated readings are not a conflict, and neither are equal values
    from different embedders (the provenance differs, the claim does not; both are listed if another reading
    conflicts). Legacy rows (source 'unset') are never part of the conflict logic. With `require_hook`, only
    groups containing a hook-written reading are examined. The result does not depend on input order."""
    return [sig for _, sig in _conflict_groups(observations, require_hook)]


def _not_assessable(signal_type: "SignalType", o: SearchObservation, basis: str, *,
                    status: str = "WATCH") -> "SearchSignal":
    return SearchSignal(
        signal_type, o.query, (o.page_url,), 0.0, status,
        proposed_action="Not assessable: relevance unknown — no query↔page relevance was measured for this "
                        "observation, so this detector cannot judge it (a low value would be a different statement)",
        evidence=(NOT_ASSESSABLE, _join(f"relevance_source={_source(o)}", _detail(o)), basis),
        site_id=getattr(o, "site_id", ""))


def is_not_assessable(signal: "SearchSignal") -> bool:
    return bool(signal.evidence) and signal.evidence[0] == NOT_ASSESSABLE


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


def _unknown_gate(o: SearchObservation, aware: bool) -> bool:
    """Should a relevance-gated detector treat this observation as 'relevance unknown'? Flag on: unknown or
    invalid relevance. Flag off: only a hook-written `unavailable:*` source (its numeric value is a placeholder
    and must never drive a detector); every legacy observation is judged on its number exactly as before."""
    return relevance_unknown(o) if aware else _kind(_source(o)) == "unavailable"


def near_win(o: SearchObservation, *, th: Thresholds = Thresholds()) -> Optional[SearchSignal]:
    """§3.2: a page ranking ~5–20 with enough impressions and strong query fit — one push could win it.
    Position alone never triggers this (§9): impressions + relevance are required."""
    aware = relevance_unknown_aware()
    if _unknown_gate(o, aware):
        if aware and o.impressions >= th.min_impressions and th.near_win_pos_min <= o.position <= th.near_win_pos_max:
            return _not_assessable(SignalType.NEAR_WIN, o, f"position {o.position:.1f} in near-win band, "
                                                           f"{o.impressions} impressions")
        return None
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
                  f"relevance {o.relevance:.2f}",) + _relevance_lines(o), site_id=o.site_id)


def emergent_intent(current: SearchObservation, *, baseline_impressions: int,
                    th: Thresholds = Thresholds()) -> Optional[SearchSignal]:
    """§3.3: Google recently started testing a page for a query that was absent/negligible before. Low
    sample + very high fit → WATCH (record, don't rewrite yet); promote only when the threshold is met."""
    aware = relevance_unknown_aware()
    if _unknown_gate(current, aware):
        if aware and baseline_impressions < th.min_impressions and current.impressions >= th.emergent_min_impressions:
            return _not_assessable(SignalType.EMERGENT_INTENT, current,
                                   f"new query/page pairing, {current.impressions} impressions "
                                   f"(baseline {baseline_impressions})")
        return None
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
                  f"(baseline {baseline_impressions}), relevance {current.relevance:.2f}",)
        + _relevance_lines(current), site_id=current.site_id)


def missing_page(o: SearchObservation, *, canonical_pages: Iterable[str] = (),
                 th: Thresholds = Thresholds()) -> Optional[SearchSignal]:
    """§3.4: a strong-fit intent ranks on a related but NON-canonical page (e.g. a blog post) — it deserves
    its own canonical page. Skip if a canonical page is already the one ranking."""
    aware = relevance_unknown_aware()
    if _unknown_gate(o, aware):
        if (aware and o.impressions >= th.min_impressions and o.page_url not in set(canonical_pages)
                and _NONCANONICAL.search(o.page_url)):
            return _not_assessable(SignalType.MISSING_PAGE, o, f"non-canonical page ranks, "
                                                               f"{o.impressions} impressions")
        return None
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
                  f"relevance {o.relevance:.2f})",) + _relevance_lines(o), site_id=o.site_id)


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
                  f"{o.impressions} impressions",) + _relevance_lines(o, used=False), site_id=o.site_id)


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
                evidence=(f"{len(pages)} pages competing for the same intent", *pages)
                + _cannibalization_relevance_lines(obs), site_id=obs[0].site_id))
    return out


def _cannibalization_relevance_lines(obs: List[SearchObservation]) -> Tuple[str, ...]:
    """Per-member relevance reading for a cannibalization signal (this detector does not need relevance; the lines
    only make the members' sources visible, so an unknown member is never mistaken for a measured one). Empty for
    observations that never touched the hook while the unknown-aware flag is off (bit-identical to before)."""
    if not (relevance_unknown_aware() or any(hook_originated(o) for o in obs)):
        return ()
    readings = sorted({(str(o.page_url), *relevance_reading(o)) for o in obs},
                      key=lambda t: (t[0], t[1], t[3], t[2] is None, 0.0 if t[2] is None else t[2]))
    lines: List[str] = []
    for page, src, val, detail in readings:
        body = f"{src} (unknown, not measured)" if val is None else f"{src} value={val:.3f}"
        lines.append(_join(f"relevance[{page}]: {body}", detail))
    unknown = sum(1 for r in readings if r[2] is None)
    if 0 < unknown < len(readings):
        lines.append(f"relevance mixed across competing pages: {len(readings) - unknown} measured, {unknown} unknown")
    return tuple(lines)
