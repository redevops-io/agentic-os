"""ERPNext catalog sensor — the operational system of record for fulfillment + pricing (Revenue plan §8).

A thin, READ-ONLY client over a real ERPNext core plus a mapper to the quote catalog, so QUOTE_FEASIBILITY
runs on real price / cost / on-hand / lead-time facts. `catalog(item_codes)` resolves each requested item to
a `CatalogItem` (list price from Item Price, unit cost from Item.valuation_rate, on-hand summed across Bins,
replenishment lead time from Item.lead_time_days). Read-only by design — nothing here mutates ERPNext (a
quotation is drafted by the approval-gated execution adapter, never by a sensor).

Self-skips cleanly (unreachable / unauthorized → empty), unit-testable with a stub, and live against a real
ERPNext with `ERPNEXT_URL` + `ERPNEXT_API_KEY` + `ERPNEXT_API_SECRET` (`erpnext_from_env`). The auth + REST
resource idioms mirror `apps/books/core.py`, the books agent's ERPNext client.
"""
from __future__ import annotations

import json as _json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from agentic_os.revenue.quote import CatalogItem


def _cents(rate: object) -> int:
    """ERPNext rates are currency units (floats); → integer minor units (cents)."""
    try:
        return int(round(float(rate or 0) * 100))
    except (TypeError, ValueError):
        return 0


@dataclass
class ErpnextClient:
    base_url: str
    api_key: str
    api_secret: str
    timeout: float = 12.0

    def _headers(self) -> dict:
        return {"Authorization": f"token {self.api_key}:{self.api_secret}", "Content-Type": "application/json"}

    def connected(self) -> bool:
        """True iff a cheap authenticated call returns the logged-in user (self-skip probe)."""
        import httpx
        try:
            r = httpx.get(f"{self.base_url}/api/method/frappe.auth.get_logged_user",
                          headers=self._headers(), timeout=4.0)
            return r.status_code == 200 and bool(r.json().get("message"))
        except Exception:  # noqa: BLE001
            return False

    def get_list(self, doctype: str, fields: List[str], filters: Optional[list] = None,
                 limit: int = 0) -> List[dict]:
        """GET an ERPNext doctype collection via the REST resource API. Empty on any failure (self-skip)."""
        import httpx
        params = {"fields": _json.dumps(fields), "limit_page_length": str(limit)}
        if filters:
            params["filters"] = _json.dumps(filters)
        try:
            r = httpx.get(f"{self.base_url}/api/resource/{doctype.replace(' ', '%20')}",
                          headers=self._headers(), params=params, timeout=self.timeout)
            return r.json().get("data", []) if r.status_code < 400 else []
        except Exception:  # noqa: BLE001
            return []

    def catalog(self, item_codes: List[str]) -> Dict[str, CatalogItem]:
        """Resolve the requested item codes to CatalogItems from live ERPNext facts. Items ERPNext doesn't
        know are simply absent from the map (the quote assessor then treats them as a hard blocker)."""
        codes = [c for c in dict.fromkeys(item_codes) if c]     # de-dup, drop blanks, preserve order
        if not codes:
            return {}
        code_filter = [["item_code", "in", codes]]
        items = self.get_list("Item", ["item_code", "item_name", "valuation_rate", "lead_time_days"],
                              filters=code_filter)
        prices = self.get_list("Item Price", ["item_code", "price_list_rate", "selling"],
                               filters=[["item_code", "in", codes], ["selling", "=", 1]])
        bins = self.get_list("Bin", ["item_code", "actual_qty"], filters=code_filter)

        # highest selling price per item (a conservative list price when several price lists exist)
        price_by_code: Dict[str, float] = {}
        for p in prices:
            c = p.get("item_code")
            if c is not None:
                price_by_code[c] = max(price_by_code.get(c, 0.0), float(p.get("price_list_rate") or 0))
        onhand_by_code: Dict[str, float] = {}
        for b in bins:
            c = b.get("item_code")
            if c is not None:
                onhand_by_code[c] = onhand_by_code.get(c, 0.0) + float(b.get("actual_qty") or 0)

        out: Dict[str, CatalogItem] = {}
        for it in items:
            code = it.get("item_code")
            if not code:
                continue
            out[code] = CatalogItem(
                item_ref=code, name=str(it.get("item_name", "") or ""),
                list_price_cents=_cents(price_by_code.get(code, 0.0)),
                unit_cost_cents=_cents(it.get("valuation_rate")),
                on_hand_qty=onhand_by_code.get(code, 0.0),
                lead_time_days=int(it.get("lead_time_days") or 0))
        return out


def erpnext_from_env() -> Optional[ErpnextClient]:
    url = os.environ.get("ERPNEXT_URL", "").rstrip("/")
    key = os.environ.get("ERPNEXT_API_KEY", "")
    secret = os.environ.get("ERPNEXT_API_SECRET", "")
    return ErpnextClient(base_url=url, api_key=key, api_secret=secret) if (url and key and secret) else None
