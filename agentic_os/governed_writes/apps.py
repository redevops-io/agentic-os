"""The writes themselves, one class per application.

The sensors in ``agentic_os.integrations`` are read-only and answer "nothing" when a call fails. A writer must not: a
failed lookup is never "not found". Every call raises one of two errors, and the difference decides what may happen next:

- :class:`NotApplied`: nothing was changed (the request never left, or the application refused it);
- :class:`OutcomeUnknown`: the request may have been carried out. It is never sent again.

Error texts name the status only. Response bodies and credentials are never repeated.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import quote


class NotApplied(Exception):
    """Nothing was changed. ``not_applied`` lets the Mission Runtime release what it reserved."""
    not_applied = True


class OutcomeUnknown(Exception):
    """The application may have made the change. Someone has to look; it is not repeated."""


class _Http:
    base_url: str
    timeout: float = 15.0
    transport: Any = None                       # test seam: an httpx transport

    def _headers(self) -> Dict[str, str]:       # pragma: no cover - every subclass defines it
        raise NotImplementedError

    def _call(self, method: str, path: str, *, params: Optional[dict] = None, body: Optional[dict] = None) -> Any:
        import httpx  # noqa: PLC0415
        writing = method != "GET"
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                r = client.request(method, f"{self.base_url}{path}", params=params, json=body, headers=self._headers())
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            raise NotApplied(f"the application could not be reached ({type(e).__name__})") from None
        except Exception as e:  # noqa: BLE001
            kind = OutcomeUnknown if writing else NotApplied
            raise kind(f"no usable answer from the application ({type(e).__name__})") from None
        if r.status_code == 404:
            raise NotFound("the application has no such record")
        if 400 <= r.status_code < 500 and r.status_code != 408:
            raise NotApplied(f"the application refused the request (HTTP {r.status_code})")
        if not 200 <= r.status_code < 300:
            raise (OutcomeUnknown if writing else NotApplied)(f"the application answered HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise (OutcomeUnknown if writing else NotApplied)("the application's answer could not be read") from None


class NotFound(NotApplied):
    pass


# ── ERPNext: accounting and procurement ──────────────────────────────────────────
#: what an agent may look up and comment on: the doctype and the fields shown for it
ERP_DOCTYPES: Dict[str, tuple] = {
    "Sales Invoice": ("name", "customer", "status", "docstatus", "posting_date", "due_date", "grand_total", "outstanding_amount", "currency"),
    "Purchase Invoice": ("name", "supplier", "status", "docstatus", "posting_date", "due_date", "grand_total", "outstanding_amount", "currency"),
    "Sales Order": ("name", "customer", "status", "docstatus", "transaction_date", "delivery_date", "grand_total", "currency"),
    "Purchase Order": ("name", "supplier", "status", "docstatus", "transaction_date", "schedule_date", "grand_total", "currency"),
    "Quotation": ("name", "party_name", "status", "docstatus", "transaction_date", "valid_till", "grand_total", "currency"),
    "Customer": ("name", "customer_name", "customer_group", "territory", "disabled"),
    "Supplier": ("name", "supplier_name", "supplier_group", "disabled"),
    "Item": ("name", "item_code", "item_name", "item_group", "stock_uom", "disabled"),
}


@dataclass
class ErpnextWriter(_Http):
    base_url: str
    api_key: str
    api_secret: str
    timeout: float = 15.0
    transport: Any = None

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"token {self.api_key}:{self.api_secret}", "Content-Type": "application/json", "Accept": "application/json"}

    @staticmethod
    def _path(doctype: str, name: str = "") -> str:
        return f"/api/resource/{quote(doctype, safe='')}" + (f"/{quote(name, safe='')}" if name else "")

    def find(self, doctype: str, *, name_contains: str = "", status: str = "", limit: int = 20) -> List[dict]:
        filters: list = []
        if name_contains:
            filters.append(["name", "like", f"%{name_contains}%"])
        if status:
            filters.append(["status", "=", status])
        params = {"fields": json.dumps(list(ERP_DOCTYPES[doctype])), "limit_page_length": str(limit), "order_by": "modified desc"}
        if filters:
            params["filters"] = json.dumps(filters)
        return list(self._call("GET", self._path(doctype), params=params).get("data") or [])

    def document(self, doctype: str, name: str) -> dict:
        data = self._call("GET", self._path(doctype, name)).get("data")
        if not isinstance(data, dict):
            raise NotApplied("the application's answer had no document")
        return data

    def comments(self, doctype: str, name: str) -> List[dict]:
        params = {"fields": json.dumps(["name", "content", "creation"]), "limit_page_length": "100", "order_by": "creation desc",
                  "filters": json.dumps([["reference_doctype", "=", doctype], ["reference_name", "=", name], ["comment_type", "=", "Comment"]])}
        return list(self._call("GET", self._path("Comment"), params=params).get("data") or [])

    def add_comment(self, doctype: str, name: str, text: str, footer: str) -> str:
        content = f"<p>{html.escape(text)}</p><p><em>{html.escape(footer)}</em></p>"
        data = self._call("POST", self._path("Comment"), body={"comment_type": "Comment", "reference_doctype": doctype,
                                                              "reference_name": name, "content": content}).get("data") or {}
        if not data.get("name"):
            raise OutcomeUnknown("the application did not say what it created")
        return str(data["name"])

    def read_list(self, doctype: str, fields: List[str], filters: Optional[list] = None, limit: int = 0) -> List[dict]:
        """A strict read for ``agentic_os.economics``: it RAISES when the collection cannot be read, so unreadable is never
        mistaken for empty."""
        from agentic_os.integrations.erpnext import ErpnextClient  # noqa: PLC0415
        params = {"fields": json.dumps(list(fields)), "limit_page_length": str(limit)}
        if filters:
            params["filters"] = json.dumps(filters)
        if doctype in ErpnextClient.CHILD_PARENT:         # ERPNext will not give a child table's columns without its parent
            params["parent"] = ErpnextClient.CHILD_PARENT[doctype]
        data = self._call("GET", self._path(doctype), params=params).get("data")
        if not isinstance(data, list):
            raise NotApplied(f"the application's list of {doctype} could not be read")
        return data

    # ── only for the payment correction (payment_correction.py): cancel, and an amendment that is drafted, then submitted ──
    def cancel(self, doctype: str, name: str) -> None:
        self._call("POST", "/api/method/frappe.client.cancel", body={"doctype": doctype, "name": name})

    def add_lineage_note(self, doctype: str, name: str, text: str, footer: str) -> None:
        """A comment on a voucher, through Frappe's own comment method: it asks only for access to that voucher, so an
        accounts-only API user can write it (creating a Comment record directly needs more). Signed as that user."""
        user = self._call("GET", "/api/method/frappe.auth.get_logged_user").get("message")
        if not user:
            raise NotApplied("the application did not say which user this is")
        self._call("POST", "/api/method/frappe.desk.form.utils.add_comment", body={
            "reference_doctype": doctype, "reference_name": name, "comment_email": str(user), "comment_by": str(user),
            "content": f"<p>{html.escape(text)}</p><p><em>{html.escape(footer)}</em></p>"})

    def create_draft(self, doctype: str, body: Mapping[str, Any]) -> str:
        data = self._call("POST", self._path(doctype), body={**body, "docstatus": 0}).get("data") or {}
        if not data.get("name"):
            raise OutcomeUnknown("the application did not say what it created")
        return str(data["name"])

    def submit(self, doctype: str, name: str) -> None:
        """Submit the draft as it stands now, so ERPNext runs its own submission rules."""
        self._call("POST", "/api/method/frappe.client.submit", body={"doc": json.dumps({**self.document(doctype, name), "doctype": doctype})})

    def create_draft_purchase_order(self, *, supplier: str, schedule_date: str, items: List[dict], company: str = "") -> str:
        """A DRAFT (``docstatus`` 0): it orders nothing and books nothing until a person submits it in ERPNext."""
        body: Dict[str, Any] = {"supplier": supplier, "schedule_date": schedule_date, "docstatus": 0,
                                "items": [{**{k: i[k] for k in ("item_code", "qty", "rate") if k in i}, "schedule_date": schedule_date}
                                          for i in items]}
        if company:
            body["company"] = company
        data = self._call("POST", self._path("Purchase Order"), body=body).get("data") or {}
        if not data.get("name"):
            raise OutcomeUnknown("the application did not say what it created")
        return str(data["name"])


# ── Chatwoot: support ────────────────────────────────────────────────────────────
@dataclass
class ChatwootWriter(_Http):
    base_url: str
    api_token: str
    account_id: str = "1"
    timeout: float = 15.0
    transport: Any = None

    def _headers(self) -> Dict[str, str]:
        return {"api_access_token": self.api_token, "Content-Type": "application/json"}

    def _acct(self) -> str:
        return f"/api/v1/accounts/{quote(str(self.account_id), safe='')}"

    def list_conversations(self, *, status: str = "open", page: int = 1) -> List[dict]:
        out = self._call("GET", f"{self._acct()}/conversations", params={"status": status, "page": page})
        return list((out.get("data") or {}).get("payload") or [])

    def conversation(self, conversation_id: int) -> dict:
        return self._call("GET", f"{self._acct()}/conversations/{int(conversation_id)}")

    def messages(self, conversation_id: int) -> List[dict]:
        out = self._call("GET", f"{self._acct()}/conversations/{int(conversation_id)}/messages")
        return list((out.get("payload") if isinstance(out, dict) else out) or [])

    def add_private_note(self, conversation_id: int, content: str) -> int:
        """Internal only: ``private`` is always true here, so the customer never sees it. There is no public reply."""
        out = self._call("POST", f"{self._acct()}/conversations/{int(conversation_id)}/messages",
                         body={"content": content, "message_type": "outgoing", "private": True})
        if not isinstance(out, dict) or not isinstance(out.get("id"), int):
            raise OutcomeUnknown("the application did not say what it created")
        return out["id"]


# ── Twenty: CRM ──────────────────────────────────────────────────────────────────
@dataclass
class TwentyWriter(_Http):
    """``shadow`` (``TWENTY_WRITE_SHADOW_MODE``, on unless set to false) previews a write and creates nothing."""
    base_url: str
    api_key: str
    shadow: bool = True
    timeout: float = 15.0
    transport: Any = None

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _list(self, kind: str, *, filter_: str = "", limit: int = 20) -> List[dict]:
        params: Dict[str, str] = {"limit": str(limit)}
        if filter_:
            params["filter"] = filter_
        return list((self._call("GET", f"/rest/{kind}", params=params).get("data") or {}).get(kind) or [])

    @staticmethod
    def _term(text: str) -> str:
        return "".join(c for c in text if c.isalnum() or c in " .-_@+")      # nothing that can extend the filter expression

    def find_companies(self, query: str = "", *, limit: int = 20) -> List[dict]:
        return self._list("companies", filter_=f"name[ilike]:%{self._term(query)}%" if query else "", limit=limit)

    def find_people(self, email: str = "", *, limit: int = 20) -> List[dict]:
        return self._list("people", filter_=f"emails.primaryEmail[ilike]:%{self._term(email)}%" if email else "", limit=limit)

    def companies_by_domain(self, host: str) -> List[dict]:
        from agentic_os.integrations.twenty_write import normalize_domain  # noqa: PLC0415
        rows = self._list("companies", filter_=f"domainName.primaryLinkUrl[ilike]:%{self._term(host)}%", limit=60)
        return [c for c in rows if normalize_domain(((c.get("domainName") or {}).get("primaryLinkUrl")) or "") == host]

    def people_by_email(self, email: str) -> List[dict]:
        rows = self._list("people", filter_=f"emails.primaryEmail[ilike]:{self._term(email)}", limit=10)
        return [p for p in rows if str((p.get("emails") or {}).get("primaryEmail") or "").lower() == email.lower()]

    def record(self, kind: str, one: str, record_id: str) -> dict:
        data = (self._call("GET", f"/rest/{kind}/{quote(record_id, safe='')}").get("data") or {}).get(one)
        if not isinstance(data, dict):
            raise NotApplied("the application's answer had no record")
        return data

    def _create(self, kind: str, result_key: str, body: dict) -> str:
        made = (self._call("POST", f"/rest/{kind}", body=body).get("data") or {}).get(result_key) or {}
        if not made.get("id"):
            raise OutcomeUnknown("the application did not say what it created")
        return str(made["id"])

    def create_company(self, name: str, host: str) -> str:
        return self._create("companies", "createCompany", {"name": name, "domainName": {"primaryLinkUrl": host}})

    def create_person(self, first: str, last: str, email: str, company_id: str = "") -> str:
        body: Dict[str, Any] = {"name": {"firstName": first, "lastName": last}, "emails": {"primaryEmail": email}}
        if company_id:
            body["companyId"] = company_id
        return self._create("people", "createPerson", body)


def writers_from_env() -> Dict[str, Any]:
    """The applications this deployment is connected to, from the same variables the sensors and dashboards read."""
    import os  # noqa: PLC0415
    from agentic_os.integrations.twenty_write import shadow_mode_enabled  # noqa: PLC0415
    env = lambda k, d="": os.environ.get(k, d).strip()  # noqa: E731
    out: Dict[str, Any] = {}
    if env("ERPNEXT_URL") and env("ERPNEXT_API_KEY") and env("ERPNEXT_API_SECRET"):
        out["erpnext"] = ErpnextWriter(env("ERPNEXT_URL").rstrip("/"), env("ERPNEXT_API_KEY"), env("ERPNEXT_API_SECRET"))
    if env("CHATWOOT_API_URL") and env("CHATWOOT_API_TOKEN"):
        out["chatwoot"] = ChatwootWriter(env("CHATWOOT_API_URL").rstrip("/"), env("CHATWOOT_API_TOKEN"), env("CHATWOOT_ACCOUNT_ID", "1") or "1")
    if env("TWENTY_BASE_URL") and env("TWENTY_API_KEY"):
        out["twenty"] = TwentyWriter(env("TWENTY_BASE_URL").rstrip("/"), env("TWENTY_API_KEY"), shadow=shadow_mode_enabled())
    from agentic_os.governed_writes.media import operators_from_env  # noqa: PLC0415 (it imports from this module)
    out.update(operators_from_env())
    return out


__all__ = ["NotApplied", "OutcomeUnknown", "NotFound", "ErpnextWriter", "ChatwootWriter", "TwentyWriter", "ERP_DOCTYPES",
           "writers_from_env"]
