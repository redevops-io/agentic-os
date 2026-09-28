"""Umami analytics sensor — pre-CRM behavioral evidence (Revenue & Content Intelligence plans §5).

A thin, dependency-light client over a real Umami core (the same API the growth-engine app uses) plus a
normalizer that turns Umami's page metrics into `AnalyticsObservation`s. This is the shared "behavior"
sensor both loops consume: the content loop measures which pages earn engagement (and whether an
intervention moved it); the revenue loop reads high-intent page activity for account re-engagement.

Live methods self-skip cleanly (unreachable core / bad creds → `connected()` False / `None` token), and the
normalizer works off any object exposing `top_pages()`, so it is unit-testable with a stub and live-testable
against a real Umami with `UMAMI_URL` / `WEBSITE_ID` / `UMAMI_ADMIN_USER` / `UMAMI_ADMIN_PASS` set.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class AnalyticsObservation:
    """A normalized behavioral reading for one page over a window."""
    page_url: str
    pageviews: int
    visitors: int = 0
    source: str = "umami"
    site_id: str = ""


def _range_ms(days: int) -> tuple:
    end = int(time.time() * 1000)
    return end - days * 86_400_000, end


@dataclass
class UmamiClient:
    base_url: str
    website_id: str
    username: str = "admin"
    password: str = ""
    timeout: float = 8.0
    _token: Optional[str] = field(default=None, repr=False)

    def connected(self) -> bool:
        import httpx
        try:
            return httpx.get(f"{self.base_url}/api/heartbeat", timeout=3.0).status_code < 400
        except Exception:  # noqa: BLE001
            return False

    def token(self) -> Optional[str]:
        if self._token:
            return self._token
        import httpx
        try:
            r = httpx.post(f"{self.base_url}/api/auth/login",
                           json={"username": self.username, "password": self.password}, timeout=self.timeout)
            if r.status_code < 400:
                self._token = r.json().get("token")
        except Exception:  # noqa: BLE001
            self._token = None
        return self._token

    def _headers(self) -> Dict[str, str]:
        tok = self.token()
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    def top_pages(self, *, days: int = 30, limit: int = 50) -> List[Dict[str, object]]:
        """Umami's URL metric: [{"x": "/path", "y": <pageviews>}, …]. Empty on any failure (self-skip)."""
        import httpx
        if not self.website_id or not self.token():
            return []
        start, end = _range_ms(days)
        try:
            r = httpx.get(f"{self.base_url}/api/websites/{self.website_id}/metrics",
                          params={"type": "url", "startAt": start, "endAt": end}, headers=self._headers(),
                          timeout=self.timeout)
            return r.json()[:limit] if r.status_code < 400 else []
        except Exception:  # noqa: BLE001
            return []

    def stats(self, *, days: int = 30) -> Dict[str, object]:
        import httpx
        if not self.website_id or not self.token():
            return {}
        start, end = _range_ms(days)
        try:
            r = httpx.get(f"{self.base_url}/api/websites/{self.website_id}/stats",
                          params={"startAt": start, "endAt": end}, headers=self._headers(), timeout=self.timeout)
            return r.json() if r.status_code < 400 else {}
        except Exception:  # noqa: BLE001
            return {}


def umami_from_env() -> Optional[UmamiClient]:
    """Build a client from the environment. Returns None when the core/site aren't configured."""
    url = os.environ.get("UMAMI_URL", "").rstrip("/")
    site = os.environ.get("WEBSITE_ID", "")
    if not url or not site:
        return None
    return UmamiClient(base_url=url, website_id=site,
                       username=os.environ.get("UMAMI_ADMIN_USER", "admin"),
                       password=os.environ.get("UMAMI_ADMIN_PASS", ""))


def collect_page_behavior(client: object, *, days: int = 30, limit: int = 50,
                          site_id: str = "", origin: str = "") -> List[AnalyticsObservation]:
    """Normalize a client's top-pages metric into AnalyticsObservations. `origin` (e.g. https://redevops.io)
    is prepended to the path so page_url matches the SearchObservation page_url from GSC."""
    out: List[AnalyticsObservation] = []
    for row in client.top_pages(days=days, limit=limit):  # type: ignore[attr-defined]
        path = str(row.get("x", "")) if isinstance(row, dict) else ""
        views = int(row.get("y", 0) or 0) if isinstance(row, dict) else 0
        if not path:
            continue
        page_url = (origin.rstrip("/") + path) if origin and path.startswith("/") else path
        out.append(AnalyticsObservation(page_url=page_url, pageviews=views, source="umami", site_id=site_id))
    return out
