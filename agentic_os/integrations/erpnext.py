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

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.normalize import normalize, register_normalizer
from agentic_os.integrations.business.operational import OperationalEvents
from agentic_os.integrations.business.supply import GoodsReceipt, PurchaseOrder, Supplier
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

    provider = "erpnext"          # OperationalConnector id (no annotation → not a dataclass field)

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

    # ── operational events (Supplier / Order Intelligence — plan §5, §7) ──────────────────────────────────
    def operational_events(self, *, supplier: str = "", limit: int = 200) -> OperationalEvents:
        """Fetch purchase orders + goods receipts as canonical operational objects, so the Supplier / Order
        families run on real ERPNext promise-vs-receipt history. Read-only, self-skips to empty. `supplier`
        scopes to one supplier; `limit` caps rows per doctype (0 ⇒ ERPNext default). Purchase-order lines are
        collapsed to the first item (one canonical PO per ERPNext PO) — a conservative first cut."""
        if not self.connected():
            return OperationalEvents()
        po_filter = [["supplier", "=", supplier]] if supplier else None
        po_rows = self.get_list("Purchase Order",
                                ["name", "supplier", "transaction_date", "schedule_date", "status",
                                 "set_warehouse"], filters=po_filter, limit=limit)
        po_names = [r["name"] for r in po_rows if r.get("name")]
        items = self.get_list("Purchase Order Item", ["parent", "item_code", "qty", "uom"],
                              filters=[["parent", "in", po_names]]) if po_names else []
        first_item: Dict[str, dict] = {}
        for it in items:
            first_item.setdefault(str(it.get("parent", "")), it)

        pos: List[PurchaseOrder] = []
        for r in po_rows:
            name = r.get("name")
            if not name:
                continue
            it = first_item.get(name, {})
            merged = {**r, "part": it.get("item_code", ""), "quantity": it.get("qty", 0),
                      "unit": it.get("uom", "")}
            obj = normalize("erpnext", merged, object_type="purchase_order", provider_ref=str(name))
            if obj is not None:
                pos.append(obj)

        pr_filter = [["supplier", "=", supplier]] if supplier else None
        pr_rows = self.get_list("Purchase Receipt", ["name", "supplier", "posting_date", "status", "is_return"],
                                filters=pr_filter, limit=limit)
        pr_index = {r["name"]: r for r in pr_rows if r.get("name")}
        pr_items = self.get_list("Purchase Receipt Item",
                                 ["parent", "purchase_order", "item_code", "received_qty", "qty"],
                                 filters=[["parent", "in", list(pr_index)]]) if pr_index else []
        receipts: List[GoodsReceipt] = []
        for it in pr_items:
            head = pr_index.get(it.get("parent"), {})
            merged = {"purchase_order": it.get("purchase_order", ""), "supplier": head.get("supplier", ""),
                      "posting_date": head.get("posting_date", ""),
                      "received_qty": it.get("received_qty") or it.get("qty") or 0,
                      "is_return": head.get("is_return")}
            obj = normalize("erpnext", merged, object_type="goods_receipt", provider_ref=str(it.get("parent", "")))
            if obj is not None:
                receipts.append(obj)

        names = {p.supplier_ref for p in pos} | {r.supplier_ref for r in receipts}
        suppliers = tuple(Supplier(prov=Provenance("erpnext", s), name=s) for s in sorted(names) if s)
        return OperationalEvents(suppliers=suppliers, purchase_orders=tuple(pos),
                                 goods_receipts=tuple(receipts))

    def fetch(self) -> OperationalEvents:
        """OperationalConnector entry point — the current operational state as canonical objects."""
        return self.operational_events()


# ── canonical normalizers: ERPNext payload → canonical supply objects (plan §4) ───────────────────────────
_PO_STATUS = {"completed": "received", "closed": "cancelled", "cancelled": "cancelled",
              "to receive": "confirmed", "to receive and bill": "confirmed"}


def _erpnext_purchase_order(d: dict, prov: Provenance) -> PurchaseOrder:
    return PurchaseOrder(
        prov=prov, supplier_ref=str(d.get("supplier", "") or ""), site=str(d.get("set_warehouse", "") or ""),
        part=str(d.get("part", "") or ""), quantity=float(d.get("quantity") or 0),
        unit=str(d.get("unit", "") or ""), promised_date=str(d.get("schedule_date", "") or ""),
        ordered_at=str(d.get("transaction_date", "") or ""),
        status=_PO_STATUS.get(str(d.get("status", "")).strip().lower(), "open"))


def _erpnext_goods_receipt(d: dict, prov: Provenance) -> GoodsReceipt:
    return GoodsReceipt(
        prov=prov, po_ref=str(d.get("purchase_order", "") or ""), supplier_ref=str(d.get("supplier", "") or ""),
        received_date=str(d.get("posting_date", "") or ""), quantity=float(d.get("received_qty") or 0),
        quality_ok=not bool(d.get("is_return")))


register_normalizer("erpnext", "purchase_order", _erpnext_purchase_order)
register_normalizer("erpnext", "goods_receipt", _erpnext_goods_receipt)


def erpnext_from_env() -> Optional[ErpnextClient]:
    url = os.environ.get("ERPNEXT_URL", "").rstrip("/")
    key = os.environ.get("ERPNEXT_API_KEY", "")
    secret = os.environ.get("ERPNEXT_API_SECRET", "")
    return ErpnextClient(base_url=url, api_key=key, api_secret=secret) if (url and key and secret) else None
