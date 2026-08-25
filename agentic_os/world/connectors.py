"""Real GTM connectors — HubSpot, Salesforce, Postmark — as governed Capability Fabric providers.

These turn the GTM world's simulated reconcile/draft into real capabilities: CRM lookup/upsert across HubSpot
and Salesforce, and email send via Postmark. They are **read-first and approval-gated by construction**:

  * ``crm.lookup_*`` are safe reads (dedup checks) — no side effect;
  * ``crm.upsert_*`` and ``mail.send`` are WRITE/EXTERNAL capabilities, so under any non-LIVE ExecutionMode
    the Capability Fabric routes them to the Outcome Simulator and nothing leaves the boundary. Only an
    explicit LIVE run that has passed the human approval gate performs the real write/send.

Credentials are read from the environment (HubSpot/Salesforce) or injected (Postmark server token, from
Vault) — never embedded here, never logged. Stdlib-only HTTP (urllib) so there is no new dependency.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

_SF_API = "v60.0"


class ConnectorError(RuntimeError):
    pass


def _http(method: str, url: str, *, headers: Dict[str, str], data: Optional[bytes] = None,
          timeout: float = 12.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:  # surface status without leaking secrets
        raise ConnectorError(f"{method} {url.split('?')[0]} -> HTTP {e.code}") from None
    except Exception as e:  # noqa: BLE001
        raise ConnectorError(f"{method} {url.split('?')[0]} -> {type(e).__name__}") from None


class HubSpotConnector:
    """HubSpot CRM via a private-app token (env HUBSPOT_API_KEY / HUBSPOT_SERVICE_KEY)."""

    def __init__(self, token: Optional[str] = None) -> None:
        # prefer the private-app token (HUBSPOT_SERVICE_KEY); HUBSPOT_API_KEY is a legacy key that 401s on Bearer
        self._token = token or os.environ.get("HUBSPOT_SERVICE_KEY") or os.environ.get("HUBSPOT_API_KEY") or ""

    def _h(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def ping(self) -> Dict[str, Any]:
        if not self._token:
            return {"connected": False, "reason": "no HUBSPOT token in env"}
        try:
            _http("GET", "https://api.hubapi.com/crm/v3/objects/companies?limit=1", headers=self._h())
            return {"connected": True}
        except ConnectorError as e:
            return {"connected": False, "reason": str(e)}

    def lookup_company(self, domain: str) -> Optional[Dict[str, Any]]:
        body = json.dumps({"filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ",
                          "value": domain}]}], "limit": 1}).encode()
        r = _http("POST", "https://api.hubapi.com/crm/v3/objects/companies/search", headers=self._h(), data=body)
        res = r.get("results") or []
        return res[0] if res else None

    def upsert_company(self, *, name: str, domain: str, props: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        existing = self.lookup_company(domain)
        payload = {"properties": {"name": name, "domain": domain, **(props or {})}}
        if existing:
            r = _http("PATCH", f"https://api.hubapi.com/crm/v3/objects/companies/{existing['id']}",
                      headers=self._h(), data=json.dumps(payload).encode())
            return {"id": r.get("id", existing["id"]), "created": False}
        r = _http("POST", "https://api.hubapi.com/crm/v3/objects/companies", headers=self._h(),
                  data=json.dumps(payload).encode())
        return {"id": r.get("id"), "created": True}

    def delete_company(self, company_id: str) -> None:   # for cleaning up a test record
        _http("DELETE", f"https://api.hubapi.com/crm/v3/objects/companies/{company_id}", headers=self._h())


class SalesforceConnector:
    """Salesforce via the OAuth 2.0 client-credentials flow (env SALESFORCE_CONSUMER_KEY / _SECRET /
    _INSTANCE_URL). Requires a Connected App with client-credentials enabled + a run-as user."""

    def __init__(self) -> None:
        self._key = os.environ.get("SALESFORCE_CONSUMER_KEY", "")
        self._secret = os.environ.get("SALESFORCE_CONSUMER_SECRET", "")
        self._instance = (os.environ.get("SALESFORCE_INSTANCE_URL", "") or "").rstrip("/")
        self._token: Optional[str] = None

    def _authenticate(self) -> str:
        if self._token:
            return self._token
        if not (self._key and self._secret and self._instance):
            raise ConnectorError("missing SALESFORCE_CONSUMER_KEY/_SECRET/_INSTANCE_URL")
        data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self._key,
                                       "client_secret": self._secret}).encode()
        r = _http("POST", f"{self._instance}/services/oauth2/token",
                  headers={"Content-Type": "application/x-www-form-urlencoded"}, data=data)
        self._token = r.get("access_token")
        if not self._token:
            raise ConnectorError("salesforce token response had no access_token")
        return self._token

    def _h(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._authenticate()}", "Content-Type": "application/json"}

    def ping(self) -> Dict[str, Any]:
        try:
            _http("GET", f"{self._instance}/services/data/{_SF_API}/limits", headers=self._h())
            return {"connected": True}
        except ConnectorError as e:
            return {"connected": False, "reason": str(e)}

    def upsert_account(self, *, name: str, website: str) -> Dict[str, Any]:
        r = _http("POST", f"{self._instance}/services/data/{_SF_API}/sobjects/Account",
                  headers=self._h(), data=json.dumps({"Name": name, "Website": website}).encode())
        return {"id": r.get("id"), "created": bool(r.get("success"))}

    def delete_account(self, account_id: str) -> None:   # for cleaning up a test record
        _http("DELETE", f"{self._instance}/services/data/{_SF_API}/sobjects/Account/{account_id}", headers=self._h())


class PostmarkConnector:
    """Transactional email via Postmark. Uses a SERVER token (from Vault vibexgen/postmark/redevops), not the
    account token — the account token cannot send. ``from_addr`` must be on a verified sender signature."""

    def __init__(self, server_token: str, *, from_addr: str = "hello@redevops.io") -> None:
        self._token = server_token
        self._from = from_addr

    def ping(self) -> Dict[str, Any]:
        try:
            _http("GET", "https://api.postmarkapp.com/server",
                  headers={"X-Postmark-Server-Token": self._token, "Accept": "application/json"})
            return {"connected": True}
        except ConnectorError as e:
            return {"connected": False, "reason": str(e)}

    def send(self, *, to: str, subject: str, body: str, tag: str = "gtm") -> Dict[str, Any]:
        payload = {"From": self._from, "To": to, "Subject": subject, "TextBody": body,
                   "Tag": tag, "MessageStream": "outbound"}
        r = _http("POST", "https://api.postmarkapp.com/email",
                  headers={"X-Postmark-Server-Token": self._token, "Content-Type": "application/json",
                           "Accept": "application/json"}, data=json.dumps(payload).encode())
        return {"message_id": r.get("MessageID"), "to": r.get("To"), "error_code": r.get("ErrorCode", 0)}
