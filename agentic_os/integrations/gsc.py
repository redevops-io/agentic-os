"""Google Search Console sensor — search-performance evidence for content signals (Content plan §3–§4).

A thin, READ-ONLY client over the Search Console API plus a mapper to `SearchObservation`, so the content
search detectors (near-win / CTR / cannibalization) run on real query→page performance. Authenticates with a
service-account JSON key (the SA must be added as a user on each Search Console property first — user
management is UI-only, not in the API). Read-only by design — it only reads search analytics.

Self-skips cleanly: no `google-auth` installed (the `gsc` extra), no key, or an unauthorized property → empty
/ `connected()` False, never raising. Unit-testable with a stub client; live against a real property with
`GSC_SA_KEY_FILE` (or `GOOGLE_APPLICATION_CREDENTIALS`) pointing at the SA key (`gsc_from_env`).
"""
from __future__ import annotations

import datetime as _dt
import math
import os
from dataclasses import dataclass, field, replace
from typing import Any, Callable, List, Optional, Tuple

from agentic_os.content.relevance import _synthetic_looking, sanitize_text
from agentic_os.content.search_signals import (
    SearchObservation, SearchSignal, SignalType, Thresholds, _conflict_groups, _kind, _source, cannibalization,
    conflict_key, ctr_opportunity, hook_originated, near_win, relevance_unknown_aware,
)

_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
_API = "https://www.googleapis.com/webmasters/v3"


def _iso(d: _dt.date) -> str:
    return d.isoformat()


@dataclass
class GscClient:
    """Read-only Search Console client. `key_file` is a service-account JSON key path."""
    key_file: str = ""
    timeout: float = 20.0
    _token: Optional[str] = field(default=None, repr=False)

    def _bearer(self) -> Optional[str]:
        if self._token:
            return self._token
        if not self.key_file or not os.path.exists(self.key_file):
            return None
        try:
            from google.oauth2 import service_account  # type: ignore
            import google.auth.transport.requests as gtr  # type: ignore
            creds = service_account.Credentials.from_service_account_file(self.key_file, scopes=[_SCOPE])
            creds.refresh(gtr.Request())
            self._token = creds.token
        except Exception:  # noqa: BLE001 — missing extra / bad key → self-skip
            self._token = None
        return self._token

    def connected(self) -> bool:
        return self._bearer() is not None

    def sites(self) -> List[str]:
        """The properties the service account can read (empty until it's added to a property)."""
        tok = self._bearer()
        if not tok:
            return []
        try:
            import requests  # type: ignore
            r = requests.get(f"{_API}/sites", headers={"Authorization": f"Bearer {tok}"}, timeout=self.timeout)
            if r.status_code >= 400:
                return []
            return [s.get("siteUrl", "") for s in r.json().get("siteEntry", []) if s.get("siteUrl")]
        except Exception:  # noqa: BLE001
            return []

    def search_analytics(self, site_url: str, *, days: int = 28, row_limit: int = 1000,
                         dimensions: Tuple[str, ...] = ("query", "page")) -> List[dict]:
        """Raw searchAnalytics rows: [{"keys": [query, page], clicks, impressions, ctr, position}, …]."""
        tok = self._bearer()
        if not tok or not site_url:
            return []
        end = _dt.date.today()
        start = end - _dt.timedelta(days=days)
        body = {"startDate": _iso(start), "endDate": _iso(end), "dimensions": list(dimensions),
                "rowLimit": row_limit}
        try:
            import requests  # type: ignore
            from urllib.parse import quote
            r = requests.post(f"{_API}/sites/{quote(site_url, safe='')}/searchAnalytics/query",
                              headers={"Authorization": f"Bearer {tok}"}, json=body, timeout=self.timeout)
            return r.json().get("rows", []) if r.status_code < 400 else []
        except Exception:  # noqa: BLE001
            return []


def to_observation(row: dict, *, days: int = 28, site_id: str = "") -> Optional[SearchObservation]:
    """Map one searchAnalytics row (dimensions query,page) → SearchObservation. None if keys are missing."""
    keys = row.get("keys") or []
    if len(keys) < 2:
        return None
    return SearchObservation(
        query=str(keys[0]), page_url=str(keys[1]), impressions=int(row.get("impressions", 0) or 0),
        clicks=int(row.get("clicks", 0) or 0), ctr=float(row.get("ctr", 0.0) or 0.0),
        position=float(row.get("position", 0.0) or 0.0), days_observed=days, site_id=site_id)


def _safe_token(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum() or ch in "_-")[:40] or "unknown"


def apply_relevance(o: SearchObservation, relevance_fn: Callable[[SearchObservation], Any]) -> SearchObservation:
    """Return `o` with `relevance`/`relevance_source`/`relevance_detail` set from `relevance_fn(o)`.

    `relevance_fn` returns an object with `value` (float in [0,1] or None), `source`, and an optional
    `provenance()` string (see `agentic_os.content.relevance.RelevanceResult`). Only an embedding-sourced value
    ('embedding:<name>@<revision>') is accepted: a synthetic/SSR buyer score or any other source is refused as
    `unavailable:invalid_result`. A failure, a None value, a malformed result or a result object whose attributes
    raise NEVER becomes 0.0 evidence: the observation keeps the placeholder 0.0 but its source is
    `unavailable:<reason>`, so a downstream detector can tell "unknown" from "measured low". The reserved prefixes
    are matched case-insensitively and written back canonically (lower case)."""
    try:
        res = relevance_fn(o)
    except Exception as exc:  # noqa: BLE001 — a hook failure must not break collection
        return replace(o, relevance_source=f"unavailable:relevance_fn_error:{_safe_token(type(exc).__name__)}")
    try:
        value = getattr(res, "value", None)
        source = sanitize_text(str(getattr(res, "source", "") or ""), 200)
        detail_fn = getattr(res, "provenance", None)
        detail = sanitize_text(str(detail_fn()), 600) if callable(detail_fn) else ""
    except Exception:  # noqa: BLE001 — a malicious/broken result object must not escape
        return replace(o, relevance_source="unavailable:invalid_result")
    low = source.lower()
    if value is None:
        if low.startswith("unavailable:") and len(source) > len("unavailable:"):
            return replace(o, relevance_source="unavailable:" + source[len("unavailable:"):].strip(),
                           relevance_detail=detail)
        return replace(o, relevance_source="unavailable:no_value")
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not 0.0 <= value <= 1.0 or not low.startswith("embedding:") or len(source) <= len("embedding:")
            or low.startswith(("ssr", "synthetic")) or _synthetic_looking(source)):
        return replace(o, relevance_source="unavailable:invalid_result")
    return replace(o, relevance=float(value), relevance_source="embedding:" + source[len("embedding:"):],
                   relevance_detail=detail)


def collect_search_observations(client: GscClient, site_url: str, *, days: int = 28,
                                site_id: str = "",
                                relevance_fn: Optional[Callable[[SearchObservation], Any]] = None
                                ) -> List[SearchObservation]:
    """Read a property's query→page performance and normalize to SearchObservations (read-only).

    `relevance_fn` (default None: output identical to before) optionally fills the semantic query↔page
    relevance per observation; it is an injected, read-only callable (see `agentic_os.content.relevance`)."""
    out: List[SearchObservation] = []
    for row in client.search_analytics(site_url, days=days):
        o = to_observation(row, days=days, site_id=site_id or site_url)
        if o is not None:
            out.append(o if relevance_fn is None else apply_relevance(o, relevance_fn))
    return out


def search_signals_from_observations(observations: List[SearchObservation], *,
                                     th: Thresholds = Thresholds()) -> List[SearchSignal]:
    """Run the self-contained search detectors (near-win, CTR opportunity, cannibalization) over a property's
    observations. (missing_page / emergent_intent need extra context and are driven separately.)

    Relevance conflicts: when the same (site, page, query) was read more than once with DIFFERING relevance (two
    measured values, or a measured value next to an unknown one) and either the unknown-aware flag is on or the
    hook wrote one of the readings, a WATCH `RELEVANCE_CONFLICT` signal lists every reading, and any PROPOSE
    near-win that depends on a conflicted, labelled reading is held at WATCH with the conflict stated. Legacy
    observations (source 'unset') never take part. Nothing is averaged or overwritten. With no hook readings and
    the flag off the output is exactly what it always was, for lists, tuples and single-use iterators alike (the
    input is walked once; as before, cannibalization sees an already-consumed iterator as empty)."""
    seen: List[SearchObservation] = []
    pairs = []
    for o in observations:
        seen.append(o)
        for sig in (near_win(o, th=th), ctr_opportunity(o, th=th)):
            if sig is not None:
                pairs.append((o, sig))
    cannibalised = cannibalization(observations, th=th)
    aware = relevance_unknown_aware()
    groups = []
    if aware or any(hook_originated(o) for o in seen):
        groups = _conflict_groups(seen, require_hook=not aware)
    held = {key for key, _ in groups}
    out: List[SearchSignal] = []
    for o, sig in pairs:
        if (held and sig.signal_type is SignalType.NEAR_WIN and sig.status == "PROPOSE"
                and _kind(_source(o)) != "unset" and conflict_key(o) in held):
            sig = replace(sig, status="WATCH", evidence=sig.evidence + (
                "relevance conflict: differing relevance readings exist for this query/page; held at "
                "WATCH (see RELEVANCE_CONFLICT)",))
        out.append(sig)
    out.extend(cannibalised)
    out.extend(sig for _, sig in groups)
    return out


def property_for_domain(client: GscClient, domain: str) -> str:
    """Resolve the GSC property that matches a bare domain — a domain property (`sc-domain:<domain>`) or a
    URL-prefix property (`https://<domain>/`). Empty when the account has no property for it."""
    props = set(client.sites())
    for cand in (f"sc-domain:{domain}", f"https://{domain}/", f"http://{domain}/",
                 f"https://{domain}", f"http://{domain}"):
        if cand in props:
            return cand
    return ""


def gsc_from_env() -> Optional[GscClient]:
    key = os.environ.get("GSC_SA_KEY_FILE") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    return GscClient(key_file=key) if key else None
