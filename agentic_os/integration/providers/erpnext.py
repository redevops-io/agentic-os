"""ERPNext (Frappe) IntegrationProvider — a public reference connector.

ERPNext is an OSS, self-hostable business system (one of our reference cores), so its connector is public — it
proves the IntegrationProvider contract against a real REST shape (Frappe's ``/api/resource/{doctype}``) before
the monetized SaaS connectors (Stripe/Salesforce/…) land in the enterprise overlay. Offline-testable via the
injected transport; auth is Frappe's ``token <api_key>:<api_secret>``.
"""
from __future__ import annotations

from typing import Any

from .http import HttpIntegrationProvider

# canonical object type → Frappe doctype
_DOCTYPE = {"invoice": "Sales Invoice", "customer": "Customer", "payment": "Payment Entry"}
# Frappe field → canonical field (on read)
_FIELD_IN = {
    "invoice": {"name": "id", "customer": "customer", "grand_total": "amount", "currency": "currency",
                "status": "status", "outstanding_amount": "outstanding"},
    "customer": {"name": "id", "customer_name": "name", "email_id": "email", "customer_group": "group"},
    "payment": {"name": "id", "party": "customer", "paid_amount": "amount", "status": "status"},
}
# canonical field → Frappe field (on write)
_FIELD_OUT = {
    "invoice": {"customer": "customer", "amount": "grand_total", "currency": "currency", "status": "status"},
    "customer": {"name": "customer_name", "email": "email_id", "group": "customer_group"},
    "payment": {"customer": "party", "amount": "paid_amount", "status": "status"},
}


class ErpnextIntegrationProvider(HttpIntegrationProvider):
    provider = "erpnext"
    _caps = ("billing.invoice.read", "billing.invoice.create", "crm.contact.read", "crm.contact.create",
             "billing.payment.read", "object.read", "object.create", "object.update")

    def _headers(self) -> dict:
        # Frappe token auth: "token <api_key>:<api_secret>"; fall back to bearer if a plain token is given.
        return {"Authorization": f"token {self._token}"} if self._token else {}

    def _doctype(self, object_type: str) -> str:
        return _DOCTYPE.get(object_type, object_type)

    def _path(self, object_type: str, external_id: str = "") -> str:
        base = f"/api/resource/{self._doctype(object_type)}"
        return f"{base}/{external_id}" if external_id else base

    def _unwrap(self, resp: Any) -> dict:
        return resp.get("data", resp) if isinstance(resp, dict) else {}

    def _normalize(self, object_type: str, raw: dict) -> dict:
        fmap = _FIELD_IN.get(object_type, {})
        out = {canon: raw.get(frappe) for frappe, canon in fmap.items() if frappe in raw}
        out.setdefault("id", raw.get("name", ""))
        return out

    def _denormalize(self, object_type: str, fields: dict) -> dict:
        omap = _FIELD_OUT.get(object_type, {})
        return {omap.get(k, k): v for k, v in fields.items() if k != "id"}
