"""agentic-billing core — the pure Lago REST client + agentic actions.

No web framework, no context-runtime: just httpx against a real Lago core. This is the
layer the FastAPI app renders from AND the Mission Runtime operator invokes, so the
capability handlers can be tested against a fake Lago without booting the whole console.

`chase_overdue(blurb=...)` takes an optional narration callback — the LLM blurb lives in
`app.py` (context-runtime), keeping this module dependency-light and deterministic.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable

import httpx

# ── config (env; seed.py writes agents/billing/.env) ─────────────────────────
# Idempotent .env load so this module is self-sufficient when imported by the operator
# (i.e. without the FastAPI app having run its own loader first).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

LAGO_API_URL = os.environ.get("LAGO_API_URL", "http://localhost:3000").rstrip("/")
LAGO_API_KEY = os.environ.get("LAGO_API_KEY", "")
LAGO_FRONT_URL = os.environ.get("LAGO_FRONT_URL", "http://localhost:80").rstrip("/")

TENANT = "Meridian Wealth Management"
SUBTITLE = "Checkout to reconciliation on a real Lago core — with a human in the loop when money moves."


# ── Lago REST client ─────────────────────────────────────────────────────────
def _headers() -> dict:
    return {"Authorization": f"Bearer {LAGO_API_KEY}", "Content-Type": "application/json"}


def lago_connected() -> bool:
    """True iff Lago's health endpoint returns 200."""
    try:
        r = httpx.get(f"{LAGO_API_URL}/health", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


def _get_all(path: str, key: str, params: dict | None = None) -> list[dict]:
    """GET a paginated Lago collection (e.g. /api/v1/invoices) and return all rows."""
    out: list[dict] = []
    page = 1
    params = dict(params or {})
    with httpx.Client(timeout=10.0) as client:
        while True:
            params.update({"page": page, "per_page": 100})
            r = client.get(f"{LAGO_API_URL}{path}", headers=_headers(), params=params)
            r.raise_for_status()
            body = r.json()
            rows = body.get(key, [])
            out.extend(rows)
            meta = body.get("meta", {})
            if not rows or page >= int(meta.get("total_pages", page)):
                break
            page += 1
    return out


# ── live data + KPIs (cached briefly) ────────────────────────────────────────
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 15.0  # seconds — keep the dashboard snappy without hammering Lago


def _money(cents: int) -> str:
    return "${:,.0f}".format((cents or 0) / 100.0)


def _invoice_state(inv: dict) -> str:
    """Collapse Lago's status/payment_status/overdue into one display state."""
    status = inv.get("status")
    pay = inv.get("payment_status")
    if inv.get("payment_overdue"):
        return "OVERDUE"
    if status == "draft":
        return "DRAFT"
    if status == "finalized" and pay == "succeeded":
        return "PAID"
    if status == "finalized" and pay == "pending":
        return "SENT"
    if pay == "failed":
        return "FAILED"
    return (status or "").upper() or "UNKNOWN"


def _days_overdue(inv: dict) -> int | None:
    due = inv.get("payment_due_date")
    if not due:
        return None
    try:
        from datetime import date

        y, m, d = (int(x) for x in due[:10].split("-"))
        delta = (date.today() - date(y, m, d)).days
        return delta if delta > 0 else 0
    except Exception:
        return None


def fetch_activity(force: bool = False) -> dict:
    """Pull REAL Lago data and compute the billing KPIs the dashboard renders."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    connected = lago_connected()
    invoices: list[dict] = []
    customers: list[dict] = []
    error = None
    if connected and LAGO_API_KEY:
        try:
            invoices = _get_all("/api/v1/invoices", "invoices")
            customers = _get_all("/api/v1/customers", "customers")
        except Exception as e:  # network / auth hiccup — surface, don't crash the page
            error = str(e)

    # KPIs straight from the live invoices.
    collected = sum(
        i.get("total_amount_cents", 0)
        for i in invoices
        if i.get("status") == "finalized" and i.get("payment_status") == "succeeded"
    )
    outstanding = sum(
        i.get("total_amount_cents", 0)
        for i in invoices
        if i.get("status") == "finalized" and i.get("payment_status") != "succeeded"
    )
    finalized = [i for i in invoices if i.get("status") == "finalized"]
    paid_ct = sum(1 for i in finalized if i.get("payment_status") == "succeeded")
    success_rate = round(100 * paid_ct / len(finalized)) if finalized else 0
    overdue = [i for i in invoices if i.get("payment_overdue")]

    # Recent invoices for the table (newest issuing_date first).
    recent = sorted(
        invoices, key=lambda i: i.get("issuing_date") or "", reverse=True
    )[:8]
    recent_rows = [
        {
            "number": i.get("number", "—"),
            "customer": (i.get("customer") or {}).get("name", "—"),
            "amount_cents": i.get("total_amount_cents", 0),
            "amount": _money(i.get("total_amount_cents", 0)),
            "state": _invoice_state(i),
            "days_overdue": _days_overdue(i) if i.get("payment_overdue") else None,
            "lago_id": i.get("lago_id"),
        }
        for i in recent
    ]

    overdue_rows = [
        {
            "number": i.get("number"),
            "customer": (i.get("customer") or {}).get("name", "—"),
            "amount": _money(i.get("total_amount_cents", 0)),
            "amount_cents": i.get("total_amount_cents", 0),
            "days_overdue": _days_overdue(i),
            "lago_id": i.get("lago_id"),
        }
        for i in overdue
    ]

    data = {
        "tenant": TENANT,
        "core": "lago",
        "connected": connected,
        "error": error,
        "front_url": LAGO_FRONT_URL,
        "kpis": [
            {"label": "Collected MTD", "value": _money(collected), "note": "finalized + paid"},
            {"label": "Outstanding", "value": _money(outstanding),
             "note": f"{len(finalized) - paid_ct} open invoice(s)"},
            {"label": "Payment success", "value": f"{success_rate}%", "note": "of finalized"},
            {"label": "Active customers", "value": str(len(customers)), "note": "in Lago"},
        ],
        "recent": recent_rows,
        "overdue": overdue_rows,
        "counts": {"invoices": len(invoices), "customers": len(customers), "overdue": len(overdue)},
    }
    _CACHE.update(ts=now, data=data)
    return data


# ── agentic actions (deterministic Lago API work) ────────────────────────────
def chase_overdue(blurb: Callable[[str], str | None] | None = None) -> dict:
    """For each overdue invoice, trigger Lago's payment retry (the dunning action).

    POST /api/v1/invoices/{lago_id}/retry_payment is the real, idempotent Lago call.
    `blurb` is an optional narration callback (the LLM one-liner lives in app.py); the
    action itself is fully deterministic and works with blurb=None.
    """
    data = fetch_activity(force=True)
    overdue = data.get("overdue", [])
    actions = []
    with httpx.Client(timeout=10.0) as client:
        for inv in overdue:
            lago_id = inv.get("lago_id")
            result = {"invoice": inv["number"], "customer": inv["customer"],
                      "amount": inv["amount"], "days_overdue": inv["days_overdue"]}
            try:
                resp = client.post(
                    f"{LAGO_API_URL}/api/v1/invoices/{lago_id}/retry_payment",
                    headers=_headers(),
                )
                result["lago_status"] = resp.status_code
                result["action"] = (
                    "retry_payment triggered (dunning reminder sent)"
                    if resp.status_code in (200, 202)
                    else f"retry_payment returned {resp.status_code}"
                )
            except Exception as e:
                result["action"] = f"error: {e}"
            actions.append(result)

    detail = "; ".join(f"{i['customer']} {i['amount']} ({i['days_overdue']}d)" for i in overdue)
    reasoning = blurb(
        "You are a billing collections agent for a wealth-management firm. In ONE sentence, "
        f"summarize sending dunning reminders / retrying payment on these {len(overdue)} overdue "
        f"invoices: {detail}. Be concrete and professional. Final answer only, no preamble."
    ) if (blurb and overdue) else None
    out = {
        "status": "done",
        "action": "chase_overdue",
        "overdue_count": len(overdue),
        "results": actions,
        "summary": f"Retried payment / sent dunning reminder on {len(actions)} overdue invoice(s) via Lago.",
    }
    if reasoning:
        out["reasoning"] = reasoning
    return out


def stage_refund(body: dict) -> dict:
    """Refunds move money OUT — never auto-executed. Stage for human approval only."""
    data = fetch_activity(force=True)
    target = body.get("invoice") or (data["recent"][0]["number"] if data["recent"] else "—")
    amount = body.get("amount", "$450")
    return {
        "status": "pending_approval",
        "action": "refund",
        "requires": "human approval",
        "summary": f"Refund of {amount} on invoice {target} is staged and awaiting human approval. "
                   "Refunds are never auto-executed by the agent.",
    }


# ── onboarding actions (create a paid subscription + its compensating undo) ───
DEFAULT_PLAN_CODE = "starter_monthly"


def _subscription_external_id(customer: str, plan: str) -> str:
    """Deterministic, idempotency-safe external_id derived from customer + plan.

    Lago treats external_id as the subscription's stable identifier, so deriving it
    (rather than minting a random one) makes create_subscription idempotent: re-running
    the onboarding mission upserts the same subscription instead of duplicating it.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", f"{customer}-{plan}".lower()).strip("-")
    return f"sub-{slug}"


def create_subscription(body: dict) -> dict:
    """Onboard a customer onto a paid plan via Lago's subscriptions API.

    POST /api/v1/subscriptions is the real, idempotent Lago call (keyed on external_id).
    Side-effecting; its compensating action is `cancel_subscription` (saga undo).
    """
    customer = body.get("customer") or "new-customer"
    plan = body.get("plan_code") or DEFAULT_PLAN_CODE
    external_id = _subscription_external_id(customer, plan)
    lago_id = None
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            f"{LAGO_API_URL}/api/v1/subscriptions",
            headers=_headers(),
            json={"subscription": {
                "external_customer_id": customer,
                "plan_code": plan,
                "external_id": external_id,
            }},
        )
        try:
            lago_id = (resp.json().get("subscription") or {}).get("lago_id")
        except Exception:  # noqa: BLE001 — non-JSON / error body: fall back to external_id
            lago_id = None
    return {
        "status": "done",
        "action": "create_subscription",
        "subscription_id": external_id or lago_id,
        "plan": plan,
    }


def cancel_subscription(body: dict) -> dict:
    """Compensating action (saga undo) for create_subscription — terminate the subscription.

    DELETE /api/v1/subscriptions/{external_id} is Lago's terminate call.
    """
    customer = body.get("customer") or "new-customer"
    plan = body.get("plan_code") or DEFAULT_PLAN_CODE
    external_id = body.get("subscription_id") or _subscription_external_id(customer, plan)
    with httpx.Client(timeout=10.0) as client:
        client.delete(f"{LAGO_API_URL}/api/v1/subscriptions/{external_id}", headers=_headers())
    return {
        "status": "done",
        "action": "cancel_subscription",
        "cancelled": True,
    }
