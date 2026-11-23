"""Twenty CRM write client — the approval-gated execution adapter `integrations.twenty`'s docstring
promises but never implements (Revenue plan §6/§17; Stage E, 2026-11-16).

`integrations.twenty.TwentyClient` is READ-ONLY by design; every write Twenty needs (a new Person/Company
record from an inbound commercial signal) was documented as living in "the approval-gated execution
adapter, never a sensor" but that adapter did not exist anywhere in the codebase until this module. The
actual approval gate is `agentic_os.integrations.twenty_operator` (via `agentic_os.approval_gate.gated`);
this module is only the HTTP write mechanics, so it stays unit-testable with a stub exactly like the read
client.

Shadow mode (`TWENTY_WRITE_SHADOW_MODE`, default "true"): since this is the first write path against a
real external CRM, every write defaults to logging a preview and returning `{"shadow": True, ...}` without
calling Twenty at all. Flip the env var to "false" only once the operator path has been reviewed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import httpx


def shadow_mode_enabled() -> bool:
    return os.environ.get("TWENTY_WRITE_SHADOW_MODE", "true").strip().lower() not in ("false", "0", "no")


@dataclass
class TwentyWriteClient:
    base_url: str
    api_key: str
    timeout: float = 12.0
    shadow: bool = True

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _find_person_by_email(self, email: str) -> Optional[dict]:
        try:
            r = httpx.get(f"{self.base_url}/rest/people", params={"filter": f"emails.primaryEmail[eq]:{email}"},
                         headers=self._headers(), timeout=self.timeout)
            if r.status_code >= 400:
                return None
            people = (r.json().get("data", {}) or {}).get("people") or []
            return people[0] if people else None
        except Exception:  # noqa: BLE001
            return None

    def upsert_person(self, *, name: str, email: str, company_id: Optional[str] = None) -> dict:
        """Create the Person if no record with `email` exists yet, else leave the existing one alone
        (a real update-in-place is left for when a real field-merge policy is designed — Stage E scope is
        "doesn't duplicate a known contact", not "reconciles every field"). Shadow mode never calls Twenty."""
        if self.shadow:
            return {"shadow": True, "would_upsert": "person", "name": name, "email": email,
                    "company_id": company_id}
        existing = self._find_person_by_email(email)
        if existing is not None:
            return {"shadow": False, "created": False, "person": existing}
        first, _, last = name.partition(" ")
        body = {"name": {"firstName": first or name, "lastName": last or ""},
               "emails": {"primaryEmail": email}}
        if company_id:
            body["companyId"] = company_id
        try:
            r = httpx.post(f"{self.base_url}/rest/people", json=body, headers=self._headers(),
                          timeout=self.timeout)
            if r.status_code >= 400:
                return {"shadow": False, "created": False, "error": f"http {r.status_code}"}
            return {"shadow": False, "created": True, "person": (r.json().get("data", {}) or {}).get("createPerson")}
        except Exception as exc:  # noqa: BLE001
            return {"shadow": False, "created": False, "error": str(exc)}

    def _find_company_by_domain(self, domain: str) -> "tuple[Optional[dict], Optional[str]]":
        """(existing company, lookup error). Twenty stores the domain as the link the record was created
        with (``acme.com``, ``https://acme.com``, ``https://www.acme.com/``), so an exact-string filter
        misses the same company entered another way and the upsert then duplicates it. Search by the bare
        host and compare normalized hosts here. A failed lookup is reported, never treated as "absent":
        creating on an unanswered lookup is how duplicates are made."""
        host = normalize_domain(domain)
        try:
            r = httpx.get(f"{self.base_url}/rest/companies",
                          params={"filter": f"domainName.primaryLinkUrl[ilike]:%{host}%", "limit": 60},
                          headers=self._headers(), timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            return None, f"lookup failed: {type(exc).__name__}"
        if r.status_code >= 400:
            return None, f"lookup failed: http {r.status_code}"
        for c in ((r.json().get("data", {}) or {}).get("companies") or []):
            if normalize_domain(((c.get("domainName") or {}).get("primaryLinkUrl")) or "") == host:
                return c, None
        return None, None

    def upsert_company(self, *, name: str, domain: str) -> dict:
        host = normalize_domain(domain)
        if not host:
            return {"shadow": self.shadow, "created": False, "error": "domain missing or malformed"}
        if self.shadow:
            return {"shadow": True, "would_upsert": "company", "name": name, "domain": host}
        existing, err = self._find_company_by_domain(host)
        if err:
            return {"shadow": False, "created": False, "error": err}
        if existing is not None:
            return {"shadow": False, "created": False, "company": existing}
        try:
            body = {"name": name, "domainName": {"primaryLinkUrl": host}}
            r = httpx.post(f"{self.base_url}/rest/companies", json=body, headers=self._headers(),
                          timeout=self.timeout)
            if r.status_code >= 400:
                return {"shadow": False, "created": False, "error": f"http {r.status_code}"}
            return {"shadow": False, "created": True, "company": (r.json().get("data", {}) or {}).get("createCompany")}
        except Exception as exc:  # noqa: BLE001
            return {"shadow": False, "created": False, "error": str(exc)}


def normalize_domain(value: str) -> str:
    """``https://www.Acme.com/about`` -> ``acme.com``. Empty when there is no plausible host."""
    v = (value or "").strip().lower()
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].split("@")[-1].split(":", 1)[0]
    if v.startswith("www."):
        v = v[4:]
    return v if "." in v and " " not in v else ""


def twenty_write_from_env() -> Optional[TwentyWriteClient]:
    url = os.environ.get("TWENTY_BASE_URL", "").rstrip("/")
    key = os.environ.get("TWENTY_API_KEY", "")
    return TwentyWriteClient(base_url=url, api_key=key, shadow=shadow_mode_enabled()) if (url and key) else None
