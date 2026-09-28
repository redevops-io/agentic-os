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
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from agentic_os.content.search_signals import (
    SearchObservation, SearchSignal, Thresholds, cannibalization, ctr_opportunity, near_win,
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


def collect_search_observations(client: GscClient, site_url: str, *, days: int = 28,
                                site_id: str = "") -> List[SearchObservation]:
    """Read a property's query→page performance and normalize to SearchObservations (read-only)."""
    out: List[SearchObservation] = []
    for row in client.search_analytics(site_url, days=days):
        o = to_observation(row, days=days, site_id=site_id or site_url)
        if o is not None:
            out.append(o)
    return out


def search_signals_from_observations(observations: List[SearchObservation], *,
                                     th: Thresholds = Thresholds()) -> List[SearchSignal]:
    """Run the self-contained search detectors (near-win, CTR opportunity, cannibalization) over a property's
    observations. (missing_page / emergent_intent need extra context and are driven separately.)"""
    out: List[SearchSignal] = []
    for o in observations:
        for sig in (near_win(o, th=th), ctr_opportunity(o, th=th)):
            if sig is not None:
                out.append(sig)
    out.extend(cannibalization(observations, th=th))
    return out


def gsc_from_env() -> Optional[GscClient]:
    key = os.environ.get("GSC_SA_KEY_FILE") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    return GscClient(key_file=key) if key else None
