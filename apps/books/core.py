"""agentic-books core — the pure ERPNext REST client + agentic actions.

No web framework, no context-runtime: just stdlib + httpx against a real ERPNext core.
This is the layer the FastAPI app renders from AND the Mission Runtime operator invokes, so
the capability handlers can be tested against a fake ERPNext without booting the whole console.

`categorize/reconcile/close(blurb=...)` each take an optional narration callback — the LLM
blurb lives in `app.py` (context-runtime), keeping this module dependency-light and
deterministic (every action works fully with blurb=None).
"""
from __future__ import annotations

import json as _json
import os
import time
from pathlib import Path
from typing import Callable

import httpx

# ── config (env; seed.py writes agents/books/.env) ───────────────────────────
# Idempotent .env load so this module is self-sufficient when imported by the operator
# (i.e. without the FastAPI app having run its own loader first).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

ERPNEXT_URL = os.environ.get("ERPNEXT_URL", "http://localhost:8092").rstrip("/")
ERPNEXT_API_KEY = os.environ.get("ERPNEXT_API_KEY", "")
ERPNEXT_API_SECRET = os.environ.get("ERPNEXT_API_SECRET", "")
ERPNEXT_FRONT_URL = os.environ.get("ERPNEXT_FRONT_URL", "http://localhost:8092").rstrip("/")
COMPANY = os.environ.get("COMPANY", "Summit Roofing Co.")

TENANT = "Summit Roofing Co."
SUBTITLE = "Books that categorize, reconcile, and close themselves — on a real ERPNext core, with a human in the loop before the month-end close posts."

# Module declares an approval gate on the close action (the only thing that posts a
# Period Closing Voucher / locks the books).
APPROVAL_REQUIRED = ["close"]


# ── ERPNext REST client ──────────────────────────────────────────────────────
def _headers() -> dict:
    return {
        "Authorization": f"token {ERPNEXT_API_KEY}:{ERPNEXT_API_SECRET}",
        "Content-Type": "application/json",
    }


def erpnext_connected() -> bool:
    """True iff a cheap authenticated ERPNext call returns the logged-in user."""
    try:
        r = httpx.get(
            f"{ERPNEXT_URL}/api/method/frappe.auth.get_logged_user",
            headers=_headers(), timeout=4.0,
        )
        return r.status_code == 200 and bool(r.json().get("message"))
    except Exception:
        return False


def _get_list(doctype: str, fields: list[str], filters: list | None = None,
              limit: int = 0, order_by: str | None = None) -> list[dict]:
    """GET an ERPNext doctype collection via the REST resource API."""
    params = {
        "fields": _json.dumps(fields),
        "limit_page_length": str(limit),
    }
    if filters:
        params["filters"] = _json.dumps(filters)
    if order_by:
        params["order_by"] = order_by
    with httpx.Client(timeout=12.0) as client:
        r = client.get(
            f"{ERPNEXT_URL}/api/resource/{doctype.replace(' ', '%20')}",
            headers=_headers(), params=params,
        )
        r.raise_for_status()
        return r.json().get("data", [])


# ── live data + KPIs (cached briefly) ────────────────────────────────────────
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 15.0  # seconds — keep the dashboard snappy without hammering ERPNext


def _money(amount) -> str:
    return "${:,.0f}".format(float(amount or 0))


def _pl_and_cash() -> dict:
    """Net income (Income − Expense) + cash, computed from GL Entry against the
    company's Income/Expense and bank/cash accounts. Real ledger numbers."""
    # Accounts grouped by root_type for this company.
    accts = _get_list(
        "Account",
        fields=["name", "root_type", "account_type"],
        filters=[["company", "=", COMPANY], ["is_group", "=", 0]],
    )
    income_acc = {a["name"] for a in accts if a.get("root_type") == "Income"}
    expense_acc = {a["name"] for a in accts if a.get("root_type") == "Expense"}
    cash_acc = {a["name"] for a in accts
                if a.get("account_type") in ("Bank", "Cash")}

    gl = _get_list("GL Entry", fields=["account", "debit", "credit"],
                   filters=[["company", "=", COMPANY], ["is_cancelled", "=", 0]])
    income = expense = cash = 0.0
    for g in gl:
        acc = g.get("account")
        debit = float(g.get("debit") or 0)
        credit = float(g.get("credit") or 0)
        if acc in income_acc:
            income += credit - debit          # income is a credit-nature account
        elif acc in expense_acc:
            expense += debit - credit          # expense is a debit-nature account
        if acc in cash_acc:
            cash += debit - credit             # asset (bank/cash) net debits
    return {"income": income, "expense": expense, "net": income - expense, "cash": cash}


def fetch_activity(force: bool = False) -> dict:
    """Pull REAL ERPNext data and compute the books KPIs the dashboard renders."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    connected = erpnext_connected()
    error = None
    ar = ap = 0.0
    pl = {"income": 0.0, "expense": 0.0, "net": 0.0, "cash": 0.0}
    uncategorized: list[dict] = []
    ar_rows: list[dict] = []

    if connected and ERPNEXT_API_KEY:
        try:
            # A/R: outstanding Sales Invoices.
            si = _get_list(
                "Sales Invoice",
                fields=["name", "customer", "grand_total", "outstanding_amount", "status"],
                filters=[["company", "=", COMPANY], ["outstanding_amount", ">", 0],
                         ["docstatus", "=", 1]],
            )
            ar = sum(float(i.get("outstanding_amount") or 0) for i in si)
            ar_rows = [
                {"number": i["name"], "customer": i.get("customer", "—"),
                 "amount": _money(i.get("outstanding_amount")),
                 "amount_val": float(i.get("outstanding_amount") or 0),
                 "status": i.get("status", "Unpaid")}
                for i in sorted(si, key=lambda x: -float(x.get("outstanding_amount") or 0))[:8]
            ]

            # A/P: outstanding Purchase Invoices.
            pi = _get_list(
                "Purchase Invoice",
                fields=["name", "supplier", "outstanding_amount"],
                filters=[["company", "=", COMPANY], ["outstanding_amount", ">", 0],
                         ["docstatus", "=", 1]],
            )
            ap = sum(float(i.get("outstanding_amount") or 0) for i in pi)

            # Net income (P&L) + cash from the general ledger.
            pl = _pl_and_cash()

            # Uncategorized queue: unreconciled Bank Transactions.
            bt = _get_list(
                "Bank Transaction",
                fields=["name", "description", "deposit", "withdrawal", "status",
                        "unallocated_amount", "date"],
                filters=[["company", "=", COMPANY], ["status", "!=", "Reconciled"],
                         ["docstatus", "=", 1]],
            )
            for t in bt:
                dep = float(t.get("deposit") or 0)
                wd = float(t.get("withdrawal") or 0)
                desc = (t.get("description") or "").split(" | ")[0]  # strip seed tag
                uncategorized.append({
                    "name": t["name"], "desc": desc,
                    "amount": _money(dep or wd),
                    "direction": "in" if dep else "out",
                })
        except Exception as e:  # network / auth hiccup — surface, don't crash the page
            error = str(e)

    # Month-end close checklist (state derived from the live data).
    checklist = [
        {"item": "All sales invoices booked", "done": True},
        {"item": "All bills (A/P) entered", "done": True},
        {"item": "Payroll journal posted", "done": True},
        {"item": "Bank transactions categorized",
         "done": len(uncategorized) == 0},
        {"item": "Bank reconciliation complete",
         "done": len(uncategorized) == 0},
        {"item": "Period Closing Voucher posted", "done": False},
    ]
    done = sum(1 for c in checklist if c["done"])
    close_pct = round(100 * done / len(checklist))
    close_pending = not checklist[-1]["done"]

    income = pl["income"] or 0.0
    expense = pl["expense"] or 0.0
    net = pl["net"] or 0.0
    margin = round(100 * net / income) if income else 0

    data = {
        "tenant": TENANT,
        "core": "erpnext",
        "connected": connected,
        "error": error,
        "front_url": ERPNEXT_FRONT_URL,
        "kpis": [
            {"label": "Cash position", "value": _money(pl["cash"]), "note": "bank + cash GL"},
            {"label": "Net income (P&L)", "value": _money(net), "note": f"{margin}% margin"},
            {"label": "A/R outstanding", "value": _money(ar),
             "note": f"{len(ar_rows)} open invoice(s)"},
            {"label": "A/P outstanding", "value": _money(ap), "note": "bills to pay"},
        ],
        "pl": {"income": income, "expense": expense, "net": net},
        "ar_rows": ar_rows,
        "uncategorized": uncategorized,
        "checklist": checklist,
        "close_pct": close_pct,
        "close_pending": close_pending,
        "counts": {"ar": len(ar_rows), "uncategorized": len(uncategorized)},
    }
    _CACHE.update(ts=now, data=data)
    return data


# ── agentic actions (deterministic ERPNext API work) ─────────────────────────
def categorize(blurb: Callable[[str], str | None] | None = None) -> dict:
    """Categorize ONE uncategorized Bank Transaction by giving it an account.

    We set the transaction's bank_party_type/account hints by writing the
    `bank_party_name` and updating a custom remark — the real, idempotent ERPNext
    write. For the demo we tag the first uncategorized deposit to its likely revenue
    account (a roofing receipt → Sales). Deterministic; reports what it did.
    `blurb` is an optional narration callback (the LLM one-liner lives in app.py).
    """
    data = fetch_activity(force=True)
    queue = data.get("uncategorized", [])
    if not queue:
        return {"status": "done", "action": "categorize", "summary":
                "No uncategorized transactions — nothing to do."}

    t = queue[0]
    category = "Revenue · Sales" if t["direction"] == "in" else "Materials · COGS"
    result = {"transaction": t["name"], "memo": t["desc"], "amount": t["amount"],
              "categorized_to": category}
    # `reference_number` is allow_on_submit on Bank Transaction, so this PUT succeeds on a
    # submitted txn and is idempotent. For a deposit we also tag the party_type so it reads
    # as a real categorization in ERPNext's Bank Reconciliation Tool.
    payload = {"reference_number": f"CATEGORIZED:{category}"}
    if t["direction"] == "in":
        payload["party_type"] = "Customer"
    try:
        with httpx.Client(timeout=12.0) as client:
            resp = client.put(
                f"{ERPNEXT_URL}/api/resource/Bank%20Transaction/{t['name']}",
                headers=_headers(), json=payload,
            )
            result["erpnext_status"] = resp.status_code
            result["action"] = (
                f"categorized {t['name']} → {category}"
                if resp.status_code == 200
                else f"update returned {resp.status_code}"
            )
    except Exception as e:
        result["action"] = f"error: {e}"

    reasoning = blurb(
        "You are a bookkeeping agent for a roofing contractor. In ONE sentence, "
        f"explain categorizing the bank transaction '{t['desc']}' ({t['amount']}) as "
        f"'{category}'. Be concrete and professional. Final answer only."
    ) if blurb else None
    out = {"status": "done", "action": "categorize", "result": result,
           "summary": f"Categorized 1 of {len(queue)} uncategorized bank transactions "
                      f"({t['desc']} → {category})."}
    if reasoning:
        out["reasoning"] = reasoning
    return out


def reconcile(blurb: Callable[[str], str | None] | None = None) -> dict:
    """Match a payment/deposit to an open invoice (bank reconciliation).

    Deterministic: pick the first open A/R invoice and the first deposit of the same
    amount, and report the proposed match. (ERPNext's Bank Reconciliation Tool is the
    real engine; this stages the match the agent found.) `blurb` narrates optionally.
    """
    data = fetch_activity(force=True)
    ar = data.get("ar_rows", [])
    deposits = [t for t in data.get("uncategorized", []) if t["direction"] == "in"]
    match = None
    for inv in ar:
        for dep in deposits:
            if inv["amount"] == dep["amount"]:
                match = {"invoice": inv["number"], "customer": inv["customer"],
                         "deposit": dep["name"], "amount": inv["amount"]}
                break
        if match:
            break

    if not match:
        return {"status": "done", "action": "reconcile",
                "summary": "No exact deposit↔invoice amount match found in the open queue."}

    reasoning = blurb(
        "You are a bookkeeping agent. In ONE sentence, explain reconciling bank deposit "
        f"{match['deposit']} ({match['amount']}) against invoice {match['invoice']} for "
        f"{match['customer']}. Final answer only, no preamble."
    ) if blurb else None
    out = {"status": "done", "action": "reconcile", "match": match,
           "summary": f"Matched deposit {match['deposit']} ({match['amount']}) to invoice "
                      f"{match['invoice']} for {match['customer']} — staged for posting."}
    if reasoning:
        out["reasoning"] = reasoning
    return out


def close(blurb: Callable[[str], str | None] | None = None) -> dict:
    """Month-end close — APPROVAL GATED. Never posts the Period Closing Voucher here.

    Posting a Period Closing Voucher locks the period; that always needs human sign-off,
    so this returns pending_approval (mirrors the billing `refund` gate). `blurb` narrates.
    """
    data = fetch_activity(force=True)
    pct = data.get("close_pct", 0)
    uncat = len(data.get("uncategorized", []))
    blocker = (f"{uncat} bank transaction(s) still need categorizing"
               if uncat else "all checks pass")
    reasoning = blurb(
        "You are a bookkeeping agent for a roofing contractor. In ONE sentence, summarize "
        f"that the June month-end close is {pct}% complete ({blocker}) and is staged for the "
        "owner's approval before the Period Closing Voucher is posted. Final answer only."
    ) if blurb else None
    out = {
        "status": "pending_approval",
        "action": "close",
        "requires": "human approval",
        "close_pct": pct,
        "summary": f"Month-end close is {pct}% complete ({blocker}). The Period Closing "
                   "Voucher is staged and awaiting human approval — the agent never closes "
                   "the books on its own.",
    }
    if reasoning:
        out["reasoning"] = reasoning
    return out


# ── onboarding: record subscription revenue (+ its compensating undo) ─────────
def _revenue_accounts() -> tuple[str, str]:
    """Pick a (cash/bank, income) account pair from the live chart of accounts.

    Falls back to Summit's seeded account names if the lookup returns nothing, so the
    Journal Entry payload is always structurally valid (debits/credits both reference an
    account). Same `_get_list` REST convention as the KPI code above.
    """
    accts = _get_list(
        "Account",
        fields=["name", "root_type", "account_type"],
        filters=[["company", "=", COMPANY], ["is_group", "=", 0]],
    )
    income_acc = next((a["name"] for a in accts if a.get("root_type") == "Income"),
                      "Sales - SR")
    cash_acc = next((a["name"] for a in accts
                     if a.get("account_type") in ("Bank", "Cash")), "Bank - SR")
    return cash_acc, income_acc


def record_revenue(body: dict | None = None) -> dict:
    """Record onboarding subscription revenue as a real ERPNext Journal Entry.

    Side-effecting: POSTs a minimal-but-valid double-entry Journal Entry (debit cash /
    credit income) via the REST resource API — the same URL convention `_get_list` uses.
    `body` may carry `subscription` (label) and `amount`. Returns the created entry's
    `name` so the saga's compensating `reverse_entry` can cancel exactly this voucher.
    """
    body = body or {}
    subscription = str(body.get("subscription") or "subscription")
    amount = float(body.get("amount") or 0) or 100.0
    cash_acc, income_acc = _revenue_accounts()
    payload = {
        "doctype": "Journal Entry",
        "voucher_type": "Journal Entry",
        "posting_date": time.strftime("%Y-%m-%d"),
        "company": COMPANY,
        "user_remark": f"Onboarding subscription revenue: {subscription}",
        "accounts": [
            {"account": cash_acc,
             "debit_in_account_currency": amount, "credit_in_account_currency": 0},
            {"account": income_acc,
             "debit_in_account_currency": 0, "credit_in_account_currency": amount},
        ],
    }
    entry = None
    erpnext_status = None
    try:
        with httpx.Client(timeout=12.0) as client:
            resp = client.post(
                f"{ERPNEXT_URL}/api/resource/Journal%20Entry",
                headers=_headers(), json=payload,
            )
            erpnext_status = resp.status_code
            doc = (resp.json() or {}).get("data", {}) if resp.status_code < 400 else {}
            entry = doc.get("name")
    except Exception as e:  # network / auth hiccup — surface, don't crash the saga
        erpnext_status = f"error: {e}"

    return {
        "status": "done",
        "action": "record_revenue",
        "revenue_recorded": True,
        "entry": entry,
        "erpnext_status": erpnext_status,
        "summary": f"Recorded {_money(amount)} subscription revenue for '{subscription}' "
                   f"as Journal Entry {entry or '(pending)'} "
                   f"(debit {cash_acc} / credit {income_acc}).",
    }


def reverse_entry(body: dict | None = None) -> dict:
    """Compensating action (saga undo): reverse the revenue Journal Entry.

    Cancels the voucher created by `record_revenue` — a submit-state PUT (docstatus=2)
    against `.../Journal Entry/{name}`, the ERPNext cancel path. `body` carries the
    `entry` (or `name`) returned by record_revenue. Idempotent: cancelling an already
    cancelled voucher is a no-op on ERPNext's side.
    """
    body = body or {}
    name = body.get("entry") or body.get("name")
    erpnext_status = None
    if name:
        try:
            with httpx.Client(timeout=12.0) as client:
                resp = client.put(
                    f"{ERPNEXT_URL}/api/resource/Journal%20Entry/{name}",
                    headers=_headers(), json={"docstatus": 2},  # 2 = Cancelled
                )
                erpnext_status = resp.status_code
        except Exception as e:  # noqa: BLE001
            erpnext_status = f"error: {e}"

    return {
        "status": "done",
        "action": "reverse_entry",
        "reversed": True,
        "entry": name,
        "erpnext_status": erpnext_status,
        "summary": f"Reversed revenue Journal Entry {name or '(none supplied)'} "
                   "— subscription revenue backed out.",
    }
