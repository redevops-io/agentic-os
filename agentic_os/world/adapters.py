"""World Adapters — project a CanonicalObject into a real OSS-core app, or an in-memory demo store.

This is the Business-OS core seam the plan calls net-new: "project one dataset into Twenty / Chatwoot / Lago
/ ERPNext / Listmonk / Postiz". Every adapter implements the same tiny contract — ``accepts(kind)``,
``available()``, ``upsert(obj) -> native_id``, ``get(native_id)`` — so the ProjectionSeeder can write a
world's entities into whichever backend is registered per app. The default is an idempotent in-memory store
(a demo runs with no core deployed), labelled SEEDED-DEMO; a configured + reachable real core is used instead
and labelled REAL-LIVE. Crucially the realism is never faked: if the live Twenty isn't reachable the seeder
falls back to in-memory and says so, rather than pretending a record landed in a system it didn't.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from runtime_contracts.world import RealismClass

from .objects import APP_CATALOG, CanonicalObject


class CoreUnavailable(RuntimeError):
    """A real core adapter could not complete a write — the registry falls back to in-memory."""


def _http_json(method: str, url: str, *, headers: Dict[str, str], payload: Optional[Dict[str, Any]] = None,
               timeout: float = 4.0) -> Dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body else {}


class InMemoryAdapter:
    """The default projection target: a deterministic in-memory record per (app, native_id). Idempotent, so
    a re-seed of the same event is a no-op and replay is stable. Realism = SEEDED-DEMO."""

    def __init__(self, app: str, store: Optional[Dict[str, Any]] = None) -> None:
        self.app = app
        self.name = f"{app}:in-memory"
        self.realism = RealismClass.SEEDED_DEMO.value
        self._store = store if store is not None else {}

    def accepts(self, kind: str) -> bool:
        return kind in APP_CATALOG.get(self.app, ())

    def available(self) -> bool:
        return True

    def upsert(self, obj: CanonicalObject) -> str:
        nid = obj.native_id(self.app)
        key = f"{self.app}:{nid}"
        if key not in self._store:
            rec = obj.to_record(self.app, nid)
            rec["projection_realism"] = self.realism
            self._store[key] = rec
        return nid

    def get(self, native_id: str) -> Optional[Dict[str, Any]]:
        return self._store.get(f"{self.app}:{native_id}")


class HttpCoreAdapter:
    """Base for a real OSS-core adapter. Reads a base URL + token from env; ``available()`` is true only when
    both are set and the core answers a health probe. ``upsert`` performs the app-specific write; a failure
    raises :class:`CoreUnavailable` so the registry degrades to in-memory rather than losing the record."""

    app = ""
    core = ""
    base_env = ""
    token_env = ""
    health_path = "/"

    def __init__(self) -> None:
        self.name = f"{self.app}:{self.core}"
        self.realism = RealismClass.REAL_LIVE.value
        self.base = os.environ.get(self.base_env, "").rstrip("/")
        self.token = os.environ.get(self.token_env, "")
        self._mirror: Dict[str, Dict[str, Any]] = {}

    def accepts(self, kind: str) -> bool:
        return kind in APP_CATALOG.get(self.app, ())

    def available(self) -> bool:
        if not (self.base and self.token):
            return False
        try:
            _http_json("GET", self.base + self.health_path, headers=self._headers(), timeout=2.0)
            return True
        except Exception:  # noqa: BLE001 — unreachable/unauthed core is simply "not available"
            return False

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _remember(self, obj: CanonicalObject, native_id: str) -> str:
        rec = obj.to_record(self.app, native_id)
        rec["projection_realism"] = self.realism
        self._mirror[native_id] = rec
        return native_id

    def get(self, native_id: str) -> Optional[Dict[str, Any]]:
        return self._mirror.get(native_id)

    def upsert(self, obj: CanonicalObject) -> str:  # pragma: no cover - overridden
        raise NotImplementedError


class LagoBillingAdapter(HttpCoreAdapter):
    app = "lago"
    core = "Lago"
    base_env = "LAGO_API_URL"
    token_env = "LAGO_API_KEY"
    health_path = "/api/v1/customers?per_page=1"

    def upsert(self, obj: CanonicalObject) -> str:
        nid = obj.native_id(self.app)
        try:
            _http_json("POST", self.base + "/api/v1/customers", headers=self._headers(),
                       payload={"customer": {"external_id": nid, "name": obj.label}})
        except Exception as e:  # noqa: BLE001
            raise CoreUnavailable(f"lago upsert failed: {type(e).__name__}") from None
        return self._remember(obj, nid)


class ChatwootAdapter(HttpCoreAdapter):
    app = "chatwoot"
    core = "Chatwoot"
    base_env = "CHATWOOT_BASE_URL"
    token_env = "CHATWOOT_API_TOKEN"

    def _headers(self) -> Dict[str, str]:
        return {"api_access_token": self.token, "Content-Type": "application/json"}

    @property
    def _account(self) -> str:
        return os.environ.get("CHATWOOT_ACCOUNT_ID", "1")

    def available(self) -> bool:
        self.health_path = f"/api/v1/accounts/{self._account}/contacts?page=1"
        return super().available()

    def upsert(self, obj: CanonicalObject) -> str:
        nid = obj.native_id(self.app)
        try:
            _http_json("POST", f"{self.base}/api/v1/accounts/{self._account}/contacts",
                       headers=self._headers(), payload={"name": obj.label, "identifier": nid})
        except Exception as e:  # noqa: BLE001
            raise CoreUnavailable(f"chatwoot upsert failed: {type(e).__name__}") from None
        return self._remember(obj, nid)


class TwentyCrmAdapter(HttpCoreAdapter):
    app = "twenty"
    core = "Twenty CRM"
    base_env = "TWENTY_BASE_URL"
    token_env = "TWENTY_API_KEY"
    health_path = "/rest/companies?limit=1"

    def upsert(self, obj: CanonicalObject) -> str:
        # Twenty's REST company has no external-id upsert and rejects unknown fields, so achieve idempotency
        # with search-then-create: find a company by name, else create one with {name} only.
        q = urllib.parse.quote(f"name[eq]:{obj.label}")
        try:
            found = _http_json("GET", f"{self.base}/rest/companies?filter={q}&limit=1", headers=self._headers())
        except Exception:  # noqa: BLE001 — a failed lookup just means we fall through to create
            found = {}
        recs = ((found.get("data") or {}).get("companies")) or found.get("companies") or []
        if recs and recs[0].get("id"):
            return self._remember(obj, recs[0]["id"])
        try:
            r = _http_json("POST", f"{self.base}/rest/companies", headers=self._headers(),
                           payload={"name": obj.label})
        except Exception as e:  # noqa: BLE001
            raise CoreUnavailable(f"twenty upsert failed: {type(e).__name__}") from None
        cid = ((r.get("data") or {}).get("createCompany") or {}).get("id") or r.get("id") or obj.native_id(self.app)
        return self._remember(obj, cid)


class ErpNextAdapter(HttpCoreAdapter):
    """ERPNext (Frappe REST) — the books/finance core. Frappe auth is a token pair sent as
    ``Authorization: token <api_key>:<api_secret>``, so this adapter needs an api-secret in addition to
    the base HttpCoreAdapter's key. Idempotency is search-then-create by the doctype's title field
    (like Twenty), so a re-seed of the same customer/opportunity does not create a duplicate."""

    app = "erpnext"
    core = "ERPNext"
    base_env = "ERPNEXT_BASE_URL"
    token_env = "ERPNEXT_API_KEY"
    secret_env = "ERPNEXT_API_SECRET"
    health_path = "/api/resource/Customer?limit_page_length=1"

    # canonical kind -> (Frappe doctype, the title field we search + create by)
    _DOCTYPE = {
        "customer": ("Customer", "customer_name"),
        "opportunity": ("Opportunity", "title"),
        "invoice": ("Sales Invoice", "title"),
        "payment": ("Payment Entry", "title"),
        "expense": ("Expense Claim", "title"),
        "ledger_entry": ("Journal Entry", "title"),
    }

    def __init__(self) -> None:
        super().__init__()
        self.secret = os.environ.get(self.secret_env, "")

    def available(self) -> bool:
        # the Frappe token pair is key AND secret; without the secret the call would 401
        return bool(self.secret) and super().available()

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"token {self.token}:{self.secret}", "Content-Type": "application/json"}

    def _resource(self, doctype: str) -> str:
        return f"{self.base}/api/resource/{urllib.parse.quote(doctype)}"

    def _find(self, doctype: str, title_field: str, label: str) -> Optional[str]:
        filters = urllib.parse.quote(json.dumps([[title_field, "=", label]]))
        try:
            r = _http_json("GET", f"{self._resource(doctype)}?filters={filters}&limit_page_length=1",
                           headers=self._headers())
        except Exception:  # noqa: BLE001 — a failed lookup just falls through to create
            return None
        rows = r.get("data") or []
        return rows[0].get("name") if rows and isinstance(rows[0], dict) else None

    def upsert(self, obj: CanonicalObject) -> str:
        doctype, title_field = self._DOCTYPE.get(obj.kind, ("Customer", "customer_name"))
        existing = self._find(doctype, title_field, obj.label)
        if existing:
            return self._remember(obj, existing)
        payload: Dict[str, Any] = {title_field: obj.label}
        if doctype == "Customer":
            payload["customer_type"] = "Company"
        try:
            r = _http_json("POST", self._resource(doctype), headers=self._headers(), payload=payload)
        except Exception as e:  # noqa: BLE001
            raise CoreUnavailable(f"erpnext upsert failed: {type(e).__name__}") from None
        name = (r.get("data") or {}).get("name") or r.get("name") or obj.native_id(self.app)
        return self._remember(obj, name)


#: real adapters the registry will prefer for an app when the core is configured + reachable.
REAL_ADAPTERS = {"lago": LagoBillingAdapter, "chatwoot": ChatwootAdapter, "twenty": TwentyCrmAdapter,
                 "erpnext": ErpNextAdapter}


class AdapterRegistry:
    """Resolves one adapter per app: an explicit override, else a configured + reachable real core, else the
    in-memory default. Instances are cached so the in-memory store persists across a run. Honest by
    construction — an app is only labelled REAL-LIVE when its core actually answered."""

    def __init__(self, overrides: Optional[Dict[str, Any]] = None, store: Optional[Dict[str, Any]] = None,
                 *, allow_real: bool = True) -> None:
        self._overrides = overrides or {}
        self._store = store if store is not None else {}
        self._allow_real = allow_real
        self._cache: Dict[str, Any] = {}

    def for_app(self, app: str) -> Any:
        if app in self._cache:
            return self._cache[app]
        adapter = self._resolve(app)
        self._cache[app] = adapter
        return adapter

    def _resolve(self, app: str) -> Any:
        if app in self._overrides:
            return self._overrides[app]
        if self._allow_real and app in REAL_ADAPTERS:
            candidate = REAL_ADAPTERS[app]()
            if candidate.available():
                return candidate
        return InMemoryAdapter(app, self._store)

    def resolved(self) -> Dict[str, str]:
        """Which adapter (+ realism) is in play per cached app — for the demo/console to show honestly."""
        return {app: {"adapter": a.name, "realism": a.realism} for app, a in self._cache.items()}
