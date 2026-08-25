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

    def lookup_contact(self, email: str) -> Optional[Dict[str, Any]]:
        body = json.dumps({"filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ",
                          "value": email}]}], "limit": 1}).encode()
        r = _http("POST", "https://api.hubapi.com/crm/v3/objects/contacts/search", headers=self._h(), data=body)
        res = r.get("results") or []
        return res[0] if res else None

    def upsert_contact(self, *, email: str, first_name: str = "", company: str = "",
                       props: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        existing = self.lookup_contact(email)
        payload = {"properties": {"email": email, "firstname": first_name, "company": company, **(props or {})}}
        if existing:
            r = _http("PATCH", f"https://api.hubapi.com/crm/v3/objects/contacts/{existing['id']}",
                      headers=self._h(), data=json.dumps(payload).encode())
            return {"id": r.get("id", existing["id"]), "created": False}
        r = _http("POST", "https://api.hubapi.com/crm/v3/objects/contacts", headers=self._h(),
                  data=json.dumps(payload).encode())
        return {"id": r.get("id"), "created": True}

    def delete_contact(self, contact_id: str) -> None:
        _http("DELETE", f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}", headers=self._h())


class HubSpotSequenceSender:
    """Cold-send via HubSpot Sales Sequences — 1:1 through your connected mailbox (your domain reputation),
    the compliant path for B2B cold outreach. ``send()`` upserts the contact, writes the evidence into
    contact properties (a sequence template renders them with tokens), and enrolls the contact in a
    configured sequence.

    REQUIRES: HubSpot **Sales Hub Enterprise**, a private-app **``sequences``** + ``crm.objects.owners.read``
    scope, a created sequence (``HUBSPOT_SEQUENCE_ID``) and a connected-inbox sender
    (``HUBSPOT_SENDER_ID``/``HUBSPOT_SENDER_EMAIL``). Contact upsert works today; enrollment 403s until the
    scope/tier are in place — the sender reports that clearly instead of silently failing."""

    def __init__(self, token: Optional[str] = None, *, sequence_id: Optional[str] = None,
                 sender_id: Optional[str] = None) -> None:
        self._hs = HubSpotConnector(token)
        self._seq = sequence_id or os.environ.get("HUBSPOT_SEQUENCE_ID", "")
        self._sender = sender_id or os.environ.get("HUBSPOT_SENDER_ID", "")

    def available(self) -> Dict[str, Any]:
        if not self._hs._token:
            return {"available": False, "reason": "no HUBSPOT token"}
        try:
            _http("GET", "https://api.hubapi.com/automation/v4/sequences?limit=1", headers=self._hs._h())
        except ConnectorError as e:
            return {"available": False, "reason": f"sequences scope/tier missing ({e}); needs Sales Hub "
                    "Enterprise + private-app 'sequences' scope"}
        if not (self._seq and self._sender):
            return {"available": False, "reason": "set HUBSPOT_SEQUENCE_ID + HUBSPOT_SENDER_ID"}
        return {"available": True}

    def send(self, *, to: str, subject: str, body: str, tag: str = "gtm") -> Dict[str, Any]:
        """Upsert the contact (works today) + enroll in the sequence (needs the scope/tier). Returns a
        Postmark-shaped result ({error_code, message_id}) so it's interchangeable in send_outreach."""
        c = self._hs.upsert_contact(email=to, props={"redevops_outreach_note": body[:900]})
        av = self.available()
        if not av["available"]:
            return {"error_code": 1, "reason": av["reason"], "contact_id": c["id"], "message_id": None}
        try:
            r = _http("POST", "https://api.hubapi.com/automation/v4/sequences/enrollments", headers=self._hs._h(),
                      data=json.dumps({"sequenceId": self._seq, "contactId": c["id"],
                                       "senderId": self._sender}).encode())
            return {"error_code": 0, "message_id": r.get("id"), "contact_id": c["id"]}
        except ConnectorError as e:
            return {"error_code": 1, "reason": str(e), "contact_id": c["id"], "message_id": None}


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


class ApolloSender:
    """Cold-send via Apollo sequences (emailer campaigns) — 1:1 through a connected mailbox on a dedicated
    sending domain, the compliant cold path. ``send()`` upserts the contact into Apollo (with the evidence
    in custom fields a sequence template renders) and adds it to a configured sequence.

    REQUIRES: a PAID Apollo plan (Free blocks all API — search/enrich/contacts 403), a connected sending
    mailbox (``APOLLO_SENDER_ACCOUNT_ID``, ideally on ``APOLLO_SENDING_DOMAIN`` e.g. get.redevops.io) and a
    created sequence (``APOLLO_SEQUENCE_ID``). Degrades with a clear reason instead of silently failing."""

    def __init__(self, key: Optional[str] = None, *, sequence_id: Optional[str] = None,
                 sender_account_id: Optional[str] = None) -> None:
        self._key = key or os.environ.get("APOLLO_API_KEY", "")
        self._seq = sequence_id or os.environ.get("APOLLO_SEQUENCE_ID", "")
        self._sender = sender_account_id or os.environ.get("APOLLO_SENDER_ACCOUNT_ID", "")
        self.sending_domain = os.environ.get("APOLLO_SENDING_DOMAIN", "")

    def _h(self) -> Dict[str, str]:
        return {"X-Api-Key": self._key, "Content-Type": "application/json", "Cache-Control": "no-cache"}

    def available(self) -> Dict[str, Any]:
        if not self._key:
            return {"available": False, "reason": "no APOLLO_API_KEY"}
        try:  # auth health does not consume credits
            h = _http("GET", "https://api.apollo.io/v1/auth/health", headers=self._h())
            if not h.get("is_logged_in"):
                return {"available": False, "reason": "apollo auth not logged in"}
        except ConnectorError as e:
            return {"available": False, "reason": str(e)}
        if not (self._seq and self._sender):
            return {"available": False, "reason": "set APOLLO_SEQUENCE_ID + APOLLO_SENDER_ACCOUNT_ID "
                    "(needs a PAID plan + a connected sending mailbox)"}
        return {"available": True}

    def send(self, *, to: str, subject: str, body: str, tag: str = "gtm") -> Dict[str, Any]:
        av = self.available()
        if not av["available"]:
            return {"error_code": 1, "reason": av["reason"], "message_id": None}
        try:
            first = ""  # the sequence template personalizes; body carried as a custom note field
            c = _http("POST", "https://api.apollo.io/v1/contacts", headers=self._h(),
                      data=json.dumps({"email": to, "first_name": first,
                                       "typed_custom_fields": {"redevops_note": body[:900]}}).encode())
            cid = (c.get("contact") or {}).get("id")
            r = _http("POST", f"https://api.apollo.io/v1/emailer_campaigns/{self._seq}/add_contact_ids",
                      headers=self._h(),
                      data=json.dumps({"contact_ids": [cid], "emailer_campaign_id": self._seq,
                                       "send_email_from_email_account_id": self._sender,
                                       "sequence_active": True}).encode())
            return {"error_code": 0, "message_id": (r.get("contacts") or [{}])[0].get("id", cid),
                    "contact_id": cid}
        except ConnectorError as e:
            return {"error_code": 1, "reason": str(e), "message_id": None}


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
