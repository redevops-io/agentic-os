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

# umami.redevops.io sits behind Cloudflare, which 403s (error 1010) a bare client; a normal browser
# User-Agent clears the bot check. Harmless against a plain Umami too.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")


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
            return httpx.get(f"{self.base_url}/api/heartbeat", headers={"User-Agent": _UA},
                             timeout=3.0).status_code < 400
        except Exception:  # noqa: BLE001
            return False

    def token(self) -> Optional[str]:
        if self._token:
            return self._token
        import httpx
        try:
            r = httpx.post(f"{self.base_url}/api/auth/login", headers={"User-Agent": _UA},
                           json={"username": self.username, "password": self.password}, timeout=self.timeout)
            if r.status_code < 400:
                self._token = r.json().get("token")
        except Exception:  # noqa: BLE001
            self._token = None
        return self._token

    def _headers(self) -> Dict[str, str]:
        tok = self.token()
        h = {"User-Agent": _UA}
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    def websites(self) -> List[Dict[str, object]]:
        """All websites the account can see: [{"id", "name", "domain", …}, …]. Empty on failure."""
        import httpx
        if not self.token():
            return []
        try:
            r = httpx.get(f"{self.base_url}/api/websites", params={"pageSize": 200},
                          headers=self._headers(), timeout=self.timeout)
            if r.status_code >= 400:
                return []
            data = r.json()
            return (data.get("data") if isinstance(data, dict) else data) or []
        except Exception:  # noqa: BLE001
            return []

    def website_id_for(self, domain: str) -> str:
        """Resolve a website id by its domain (so a caller can scan a site without hard-coding an id)."""
        for w in self.websites():
            if str(w.get("domain", "")).lower() == domain.lower():
                return str(w.get("id", ""))
        return ""

    def top_pages(self, *, days: int = 30, limit: int = 50) -> List[Dict[str, object]]:
        """Umami's top-pages metric: [{"x": "/path", "y": <pageviews>}, …]. Empty on any failure (self-skip).
        The metric type is ``path`` on current Umami and ``url`` on older builds, so try both."""
        import httpx
        if not self.website_id or not self.token():
            return []
        start, end = _range_ms(days)
        for metric_type in ("path", "url"):
            try:
                r = httpx.get(f"{self.base_url}/api/websites/{self.website_id}/metrics",
                              params={"type": metric_type, "startAt": start, "endAt": end},
                              headers=self._headers(), timeout=self.timeout)
                if r.status_code < 400:
                    rows = r.json()
                    if rows:
                        return rows[:limit]
            except Exception:  # noqa: BLE001
                continue
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
    if not url:
        return None
    # website id is optional now — a caller can resolve it by domain (website_id_for)
    return UmamiClient(
        base_url=url, website_id=os.environ.get("WEBSITE_ID", ""),
        username=os.environ.get("UMAMI_USERNAME") or os.environ.get("UMAMI_ADMIN_USER", "admin"),
        password=os.environ.get("UMAMI_PASSWORD") or os.environ.get("UMAMI_ADMIN_PASS", ""))


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
