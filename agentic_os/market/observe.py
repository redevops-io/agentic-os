"""Website / funnel observation (Market-Intelligence plan §6, Phase 1) — the first real MarketSourceAdapter.

A READ-ONLY sensor over a company's PUBLIC site: fetch a handful of acquisition-relevant pages (home, pricing,
demo, signup, product), snapshot each (title, page kind, content hash for change detection), and extract the
funnel elements visible in the HTML — CTAs, offers, and forms — as canonical :mod:`.contracts` objects. It is a
sensor only: it never logs in, submits a form, or follows a signup. Deterministic HTML parsing (stdlib), no
model; fetching is injectable so the extraction is fully testable offline, and it self-skips (unreachable /
non-HTML → nothing) so a partly-reachable target degrades to fewer observations rather than an error.

The competitor LIST and any credentialed capture infrastructure are ReDevOps specifics (private overlay); this
generic observer is the public capability.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Dict, List, Optional, Protocol, Tuple

from .contracts import CTA, FormObservation, MarketObservations, Offer, PageSnapshot, Provenance, TrackedCompany

# acquisition paths worth observing on a company site (plan §6 "landing-page discovery")
DEFAULT_PATHS: Tuple[str, ...] = ("", "/pricing", "/plans", "/demo", "/get-started", "/signup", "/product")

_ACTIONS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("trial", ("free trial", "start free", "try free", "start trial", "try for free", "start your free")),
    ("demo", ("get a demo", "book a demo", "request a demo", "see a demo", "watch demo", "live demo")),
    ("signup", ("sign up", "signup", "get started", "start now", "create account", "join free", "try now")),
    ("book", ("book a call", "schedule a", "book now", "book time")),
    ("contact", ("contact sales", "talk to sales", "contact us", "get in touch", "talk to us")),
    ("download", ("download", "get the guide", "free download", "get the template")),
    ("subscribe", ("subscribe", "join the newsletter", "sign up for updates")),
)
_OFFERS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("trial", ("free trial", "day trial", "days free", "trial")),
    ("freemium", ("free plan", "free tier", "free forever", "freemium", "start for free", "free to use")),
    ("discount", ("% off", "discount", "save ", "limited time", "offer ends")),
    ("lead_magnet", ("template", "calculator", "free guide", "ebook", "e-book", "checklist", "whitepaper",
                     "white paper", "free tool", "cheat sheet")),
)


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self.texts: List[str] = []
        self.anchors: List[str] = []          # visible text of links/buttons
        self._cur: List[str] = []
        self._cur_kind = ""
        self.forms: List[List[str]] = []      # each form: its input names/types
        self._form: Optional[List[str]] = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag in ("script", "style"):
            self._skip += 1
        elif tag in ("a", "button"):
            self._cur = []
            self._cur_kind = tag
        elif tag == "form":
            self._form = []
        elif tag == "input" and self._form is not None:
            self._form.append((a.get("name") or a.get("type") or "field").lower())

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in ("a", "button") and self._cur_kind == tag:
            t = " ".join(self._cur).strip()
            if t:
                self.anchors.append(t)
            self._cur_kind = ""
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None

    def handle_data(self, data):
        d = data.strip()
        if not d or self._skip:
            return
        if self._in_title:
            self.title += d
        if self._cur_kind:
            self._cur.append(d)
        self.texts.append(d)

    def text(self) -> str:
        return " ".join(self.texts)


@dataclass(frozen=True)
class FetchedPage:
    url: str
    status: int
    html: str = ""


class PageFetcher(Protocol):
    def get(self, url: str) -> FetchedPage: ...


@dataclass
class HttpPageFetcher:
    """Real fetcher (httpx). Self-skips to an empty page on any failure or non-HTML response."""
    timeout: float = 10.0
    user_agent: str = "ReDevOps-MarketObserver/0.1 (+https://redevops.io)"

    def get(self, url: str) -> FetchedPage:
        try:
            import httpx
            r = httpx.get(url, headers={"User-Agent": self.user_agent}, timeout=self.timeout,
                          follow_redirects=True)
            ctype = r.headers.get("content-type", "")
            html = r.text if (r.status_code < 400 and "html" in ctype) else ""
            return FetchedPage(url=str(r.url), status=r.status_code, html=html)
        except Exception:  # noqa: BLE001
            return FetchedPage(url=url, status=0)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _page_kind(path: str) -> str:
    p = path.rstrip("/").lower()
    if p in ("", "/"):
        return "home"
    if "pricing" in p or "plans" in p:
        return "pricing"
    if "demo" in p:
        return "demo"
    if any(k in p for k in ("signup", "sign-up", "register", "get-started", "trial")):
        return "form"
    return "landing"


def _match(text: str, table) -> List[str]:
    low = text.lower()
    return [key for key, kws in table if any(k in low for k in kws)]


def _cta_action(anchor: str) -> str:
    for action, kws in _ACTIONS:
        if any(k in anchor.lower() for k in kws):
            return action
    return ""


def _form_purpose(fields: List[str], page_text: str) -> str:
    fs = set(fields)
    if any("pass" in f for f in fs):
        return "signup"
    low = page_text.lower()
    if "demo" in low and any("email" in f for f in fs):
        return "demo_request"
    if any(k in low for k in ("calculator", "quiz")):
        return "quiz"
    if any("email" in f for f in fs) and len(fs) <= 2:
        return "newsletter"
    return "contact" if len(fs) >= 3 else "signup"


@dataclass
class WebsiteSourceAdapter:
    """Observe a company's public acquisition pages. `provider='website'`; `paths` are the relative pages to
    try (per company you can widen this later from discovered links)."""
    fetcher: Optional[PageFetcher] = None
    paths: Tuple[str, ...] = DEFAULT_PATHS
    provider: str = "website"

    def connected(self) -> bool:
        return self.fetcher is not None

    def _origin(self, company: TrackedCompany) -> str:
        d = (company.domain or "").strip()
        if not d:
            return ""
        return d if d.startswith("http") else f"https://{d}"

    def observe(self, company: TrackedCompany) -> MarketObservations:
        if self.fetcher is None:
            return MarketObservations(companies=(company,))
        origin = self._origin(company)
        if not origin:
            return MarketObservations(companies=(company,))
        co = company.prov.provider_ref or company.domain
        snaps: List[PageSnapshot] = []
        ctas: List[CTA] = []
        offers: List[Offer] = []
        forms: List[FormObservation] = []
        seen_offer_kinds: set = set()
        for path in self.paths:
            url = origin.rstrip("/") + path
            page = self.fetcher.get(url)
            if not page.status or not page.html:
                continue
            p = _Page()
            try:
                p.feed(page.html)
            except Exception:  # noqa: BLE001
                continue
            text = p.text()
            ref = f"{co}:{path or '/'}"
            snaps.append(PageSnapshot(
                prov=Provenance(provider=self.provider, provider_ref=ref, evidence_refs=(url,)),
                company_ref=co, url=page.url or url, page_kind=_page_kind(path), title=p.title.strip()[:200],
                content_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(), captured_at=_now_iso()))
            # CTAs — de-dup by (action, text)
            seen_cta: set = set()
            for anchor in p.anchors:
                action = _cta_action(anchor)
                if action and (action, anchor[:60]) not in seen_cta:
                    seen_cta.add((action, anchor[:60]))
                    ctas.append(CTA(prov=Provenance(self.provider, f"{ref}#cta:{len(ctas)}"),
                                    page_ref=ref, text=anchor[:120], action=action))
            # offers — one per kind across the company
            for kind in _match(text, _OFFERS):
                if kind not in seen_offer_kinds:
                    seen_offer_kinds.add(kind)
                    offers.append(Offer(prov=Provenance(self.provider, f"{co}:offer:{kind}"),
                                        page_ref=ref, description=kind, kind=kind))
            # forms
            for i, fields in enumerate(p.forms):
                if fields:
                    forms.append(FormObservation(
                        prov=Provenance(self.provider, f"{ref}#form:{i}"), page_ref=ref,
                        fields=tuple(dict.fromkeys(fields)), purpose=_form_purpose(fields, text)))
        return MarketObservations(companies=(company,), snapshots=tuple(snaps), ctas=tuple(ctas),
                                  offers=tuple(offers), forms=tuple(forms))
