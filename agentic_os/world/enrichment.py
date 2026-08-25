"""Contact enrichment + email verification — provider-agnostic (Apollo / Hunter / Clearbit).

Turns a company domain + a person's name into a *verified* business email, which is the gate the outreach
engine needs before it can send (a lead with no verified email stays NO_EMAIL / NEEDS_MORE_EVIDENCE). One
capability, three adapters; the provider is chosen by ``REDEVOPS_ENRICHMENT_PROVIDER`` or auto-detected from
whichever API key is set. No key → degrades to "not configured" (never a fabricated address). Stdlib HTTP;
keys read from env, never logged.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


class EnrichmentError(RuntimeError):
    pass


def _err(url: str, e: Exception) -> "EnrichmentError":
    import urllib.error  # noqa: PLC0415
    if isinstance(e, urllib.error.HTTPError):
        try:
            msg = (json.loads(e.read().decode()).get("error") or "")[:140]
        except Exception:  # noqa: BLE001
            msg = ""
        return EnrichmentError(f"{url.split('?')[0]} -> HTTP {e.code}" + (f": {msg}" if msg else ""))
    return EnrichmentError(f"{url.split('?')[0]} -> {type(e).__name__}")


def _get(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 10.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "redevops-gtm"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except Exception as e:  # noqa: BLE001
        raise _err(url, e) from None


def _post(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: float = 10.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, method="POST", headers=headers, data=json.dumps(payload).encode())
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except Exception as e:  # noqa: BLE001
        raise _err(url, e) from None


class HunterProvider:
    name = "hunter"

    def __init__(self, key: Optional[str] = None) -> None:
        self._key = key or os.environ.get("HUNTER_API_KEY", "")

    def configured(self) -> bool:
        return bool(self._key)

    def find_email(self, *, domain: str, first_name: str, last_name: str) -> Dict[str, Any]:
        q = urllib.parse.urlencode({"domain": domain, "first_name": first_name, "last_name": last_name,
                                    "api_key": self._key})
        d = _get(f"https://api.hunter.io/v2/email-finder?{q}").get("data", {})
        return {"email": d.get("email"), "confidence": (d.get("score") or 0) / 100.0, "verified": None}

    def verify_email(self, email: str) -> Dict[str, Any]:
        d = _get(f"https://api.hunter.io/v2/email-verifier?{urllib.parse.urlencode({'email': email, 'api_key': self._key})}").get("data", {})
        status = d.get("status")
        return {"status": status, "score": (d.get("score") or 0) / 100.0,
                "verified": status in ("valid", "accept_all")}


class ApolloProvider:
    name = "apollo"

    def __init__(self, key: Optional[str] = None) -> None:
        self._key = key or os.environ.get("APOLLO_API_KEY", "")

    def configured(self) -> bool:
        return bool(self._key)

    def _h(self) -> Dict[str, str]:
        return {"Content-Type": "application/json", "Cache-Control": "no-cache", "X-Api-Key": self._key}

    def find_email(self, *, domain: str, first_name: str, last_name: str) -> Dict[str, Any]:
        r = _post("https://api.apollo.io/v1/people/match",
                  {"first_name": first_name, "last_name": last_name, "domain": domain,
                   "reveal_personal_emails": False}, self._h())
        p = r.get("person") or {}
        status = p.get("email_status")
        return {"email": p.get("email"), "confidence": 0.8 if status == "verified" else 0.4,
                "verified": status == "verified"}

    def verify_email(self, email: str) -> Dict[str, Any]:
        # Apollo returns email_status on match; a standalone verify isn't exposed the same way
        return {"status": "unknown", "score": 0.5, "verified": None}

    def search_people(self, *, domain: str, titles: "list[str]", limit: int = 3) -> "list[Dict[str, Any]]":
        """Find the buying group by company + title (org domain + titles → people + emails). This is the
        right resolution for GTM: from 'company + role' to 'named person + verified email'."""
        r = _post("https://api.apollo.io/v1/mixed_people/search",
                  {"q_organization_domains": domain, "person_titles": titles, "per_page": limit}, self._h())
        out = []
        for p in (r.get("people") or [])[:limit]:
            out.append({"name": (p.get("name") or f"{p.get('first_name','')} {p.get('last_name','')}").strip(),
                        "first_name": p.get("first_name", ""), "title": p.get("title", ""),
                        "email": p.get("email"), "verified": p.get("email_status") == "verified"})
        return out


class ClearbitProvider:
    name = "clearbit"

    def __init__(self, key: Optional[str] = None) -> None:
        self._key = key or os.environ.get("CLEARBIT_API_KEY", "")

    def configured(self) -> bool:
        return bool(self._key)

    def find_email(self, *, domain: str, first_name: str, last_name: str) -> Dict[str, Any]:
        # Clearbit Prospector: search people at a domain (returns candidates); pick a role match upstream
        q = urllib.parse.urlencode({"domain": domain, "name": f"{first_name} {last_name}"})
        r = _get(f"https://prospector.clearbit.com/v1/people/search?{q}",
                 headers={"Authorization": f"Bearer {self._key}"})
        people = r if isinstance(r, list) else r.get("results", [])
        email = (people[0].get("email") if people else None)
        return {"email": email, "confidence": 0.6 if email else 0.0, "verified": None}

    def verify_email(self, email: str) -> Dict[str, Any]:
        return {"status": "unknown", "score": 0.5, "verified": None}


_PROVIDERS = {"hunter": HunterProvider, "apollo": ApolloProvider, "clearbit": ClearbitProvider}


def get_provider(name: Optional[str] = None):
    """The configured enrichment provider: explicit name, or REDEVOPS_ENRICHMENT_PROVIDER, or the first with
    a key set. Returns None if none is configured (so callers keep leads at NO_EMAIL, never fabricate one)."""
    chosen = (name or os.environ.get("REDEVOPS_ENRICHMENT_PROVIDER", "")).lower()
    if chosen in _PROVIDERS:
        p = _PROVIDERS[chosen]()
        return p if p.configured() else None
    for cls in _PROVIDERS.values():
        p = cls()
        if p.configured():
            return p
    return None


def resolve_verified_email(*, domain: str, first_name: str, last_name: str,
                           provider: Optional[Any] = None) -> Dict[str, Any]:
    """Find + verify a business email. Returns {email, verified, provider, confidence} or email=None. A email
    is only usable for outreach when ``verified`` is True (verified) or the provider gives high confidence."""
    p = provider or get_provider()
    if p is None:
        return {"email": None, "verified": False, "provider": None, "reason": "no enrichment provider configured"}
    try:
        found = p.find_email(domain=domain, first_name=first_name, last_name=last_name)
        email = found.get("email")
        if not email:
            return {"email": None, "verified": False, "provider": p.name, "reason": "no email found"}
        ver = p.verify_email(email)
        verified = bool(ver.get("verified")) or (found.get("confidence", 0) >= 0.8)
        return {"email": email, "verified": verified, "provider": p.name,
                "confidence": max(found.get("confidence", 0), ver.get("score", 0))}
    except EnrichmentError as e:
        return {"email": None, "verified": False, "provider": p.name, "reason": str(e)}


def resolve_contact(*, domain: str, titles: "list[str]", provider: Optional[Any] = None) -> Dict[str, Any]:
    """From company domain + buying-group titles → a named contact with a verified email (Apollo people
    search). Returns {name, email, verified, provider} or email=None when no provider/contact/email exists
    — the outreach engine then keeps the lead at NO_EMAIL rather than fabricating an address."""
    p = provider or get_provider()
    if p is None:
        return {"email": None, "verified": False, "provider": None, "reason": "no enrichment provider configured"}
    if not hasattr(p, "search_people"):
        return {"email": None, "verified": False, "provider": p.name, "reason": f"{p.name} has no people-search"}
    try:
        people = p.search_people(domain=domain, titles=titles)
        for person in people:                       # prefer a verified email
            if person.get("email") and person.get("verified"):
                return {"name": person["name"], "first_name": person.get("first_name", ""),
                        "email": person["email"], "verified": True, "provider": p.name}
        for person in people:                       # else any email (unverified)
            if person.get("email"):
                return {"name": person["name"], "first_name": person.get("first_name", ""),
                        "email": person["email"], "verified": False, "provider": p.name}
        return {"email": None, "verified": False, "provider": p.name, "reason": "no contact email found"}
    except EnrichmentError as e:
        return {"email": None, "verified": False, "provider": p.name, "reason": str(e)}
