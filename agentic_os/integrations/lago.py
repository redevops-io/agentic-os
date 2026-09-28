"""Lago billing sensor — metered usage vs plan boundary (Revenue plan §6, §21).

A thin, READ-ONLY client over a real Lago core plus a scan that turns usage-vs-plan into revenue leakage:
for each active subscription, current metered usage is compared to the plan's recurring amount, and a healthy
account at/over the threshold surfaces as an EXPANSION_OPPORTUNITY (§6 — usage approaching the plan boundary).
Read-only by design — nothing here changes a plan or bills a customer (a plan-expansion proposal is drafted by
the approval-gated execution adapter, never a sensor).

Self-skips cleanly (unreachable / unauthorized → empty), unit-testable with a stub, and live against a real
Lago with `LAGO_API_URL` + `LAGO_API_KEY` (`lago_from_env`). Auth (Bearer) + `/api/v1` idioms mirror
`apps/billing/core.py`, the billing agent's Lago client.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, List, Optional

from agentic_os.revenue.leakage import RevenueLeakage, expansion_opportunity


@dataclass
class LagoClient:
    base_url: str
    api_key: str
    timeout: float = 10.0

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def connected(self) -> bool:
        import httpx
        try:
            return httpx.get(f"{self.base_url}/health", timeout=4.0).status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        import httpx
        if not self.api_key:
            return {}
        try:
            r = httpx.get(f"{self.base_url}{path}", headers=self._headers(), params=params or {},
                          timeout=self.timeout)
            return r.json() if r.status_code < 400 else {}
        except Exception:  # noqa: BLE001
            return {}

    def subscriptions(self, *, status: str = "active", max_pages: int = 20) -> List[dict]:
        """Active subscriptions (following pagination). Empty on any failure (self-skip)."""
        out: List[dict] = []
        for page in range(1, max_pages + 1):
            data = self._get("/api/v1/subscriptions", {"status[]": status, "page": page, "per_page": 50})
            rows = data.get("subscriptions", []) or []
            out.extend(rows)
            meta = data.get("meta", {}) or {}
            if not rows or page >= int(meta.get("total_pages", page)):
                break
        return out

    def plan(self, code: str) -> dict:
        return self._get(f"/api/v1/plans/{code}").get("plan", {}) or {}

    def current_usage(self, external_customer_id: str, external_subscription_id: str) -> dict:
        data = self._get(f"/api/v1/customers/{external_customer_id}/current_usage",
                         {"external_subscription_id": external_subscription_id})
        return data.get("customer_usage", {}) or {}


def scan_expansion(client: LagoClient, *, ratio_threshold: float = 0.8,
                   account_healthy: Optional[Callable[[str], bool]] = None) -> List[RevenueLeakage]:
    """Read active subscriptions and surface usage-approaching-plan-boundary as EXPANSION_OPPORTUNITY.

    usage_ratio = current metered usage / the plan's recurring amount (the reference boundary). `account_
    healthy(external_customer_id) -> bool` gates it (default healthy); a real binding checks for overdue
    invoices. Plan amounts are cached across subscriptions so the scan stays cheap.
    """
    healthy = account_healthy or (lambda _s: True)
    plan_amount: dict = {}
    out: List[RevenueLeakage] = []
    for sub in client.subscriptions(status="active"):
        ext_cust = sub.get("external_customer_id")
        ext_sub = sub.get("external_id")
        code = sub.get("plan_code")
        if not (ext_cust and ext_sub and code):
            continue
        if code not in plan_amount:
            plan_amount[code] = int(client.plan(code).get("amount_cents") or 0)
        boundary = plan_amount[code]
        if boundary <= 0:
            continue
        used = int(client.current_usage(ext_cust, ext_sub).get("total_amount_cents") or 0)
        leak = expansion_opportunity(
            ext_cust, usage_ratio=used / boundary, account_healthy=healthy(ext_cust),
            ratio_threshold=ratio_threshold, amount_cents=used,
            observation_refs=(f"lago:subscription:{ext_sub}",))
        if leak is not None:
            out.append(leak)
    return out


def lago_from_env() -> Optional[LagoClient]:
    url = os.environ.get("LAGO_API_URL", "").rstrip("/")
    key = os.environ.get("LAGO_API_KEY", "")
    return LagoClient(base_url=url, api_key=key) if (url and key) else None
