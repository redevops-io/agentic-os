"""agentic-books core — the pure ERPNext REST client + agentic actions.

No web framework, no context-runtime: just stdlib + httpx against a real ERPNext core.
This is the layer the FastAPI app renders from AND the Mission Runtime operator invokes, so
the capability handlers can be tested against a fake ERPNext without booting the whole console.

`categorize/reconcile/close(blurb=...)` each take an optional narration callback — the LLM
blurb lives in `app.py` (context-runtime), keeping this module dependency-light and
deterministic (every action works fully with blurb=None).
"""
from __future__ import annotations

import hashlib
import threading
import json as _json
import os
import re
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

import httpx
from urllib.parse import quote

from agentic_os.mission.operator_sdk import pending_approval

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
COMPANY = os.environ.get("COMPANY", "Meridian Wealth Management")

TENANT = "Meridian Wealth Management"
SUBTITLE = "Books that categorize, reconcile, and close themselves — on a real ERPNext core, with a human in the loop before the month-end close posts."

# Module declares an approval gate on the close action (the only thing that posts a
# Period Closing Voucher / locks the books).
APPROVAL_REQUIRED = ["close"]


CATEGORY_TAG = "CATEGORIZED:"


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


def _cents(amount) -> int:
    """Exact integer cents for an ERPNext amount (half-up), so matching never depends on display rounding."""
    try:
        return int((Decimal(str(amount or 0)) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return 0


COMPANY_CURRENCY = os.environ.get("BOOKS_CURRENCY", "USD").upper()   # the books' ledger currency
MAX_REVENUE = Decimal("1000000000")          # per-entry upper bound
_DOC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,139}$")


def _valid_doc_name(name) -> bool:
    return isinstance(name, str) and bool(_DOC_NAME.match(name)) and ".." not in name


def parse_revenue_amount(value) -> Decimal:
    """Strict positive amount for a journal entry: Decimal, finite, > 0, bounded, rounded to cents.
    Missing/zero/negative/non-finite/garbage values raise ValueError (there is no default amount)."""
    if value is None or isinstance(value, (bool, list, dict)):
        raise ValueError(f"invalid amount {value!r}")
    try:
        d = Decimal(str(value).strip().replace(",", "").lstrip("$"))
        if not d.is_finite() or d <= 0 or d > MAX_REVENUE * 10:
            raise ValueError(f"amount must be a finite positive number within range, got {value!r}")
        d = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except ArithmeticError:
        raise ValueError(f"invalid amount {value!r}") from None
    if d <= 0 or d > MAX_REVENUE:
        raise ValueError(f"amount out of range: {value!r}")
    return d


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
    uncategorized: list[dict] = []     # unreconciled AND not yet tagged by the categorizer
    unreconciled: list[dict] = []      # every non-Reconciled bank transaction
    draft_counts = {"sales_invoices": 0, "purchase_invoices": 0, "journal_entries": 0}
    existing_pcv = None
    fiscal_year = time.strftime("%Y")
    ar_rows: list[dict] = []
    checks_available = False

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
                 "amount_cents": _cents(i.get("outstanding_amount")),
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
                        "unallocated_amount", "date", "reference_number"],
                filters=[["company", "=", COMPANY], ["status", "!=", "Reconciled"],
                         ["docstatus", "=", 1]],
            )
            for t in bt:
                dep = float(t.get("deposit") or 0)
                wd = float(t.get("withdrawal") or 0)
                desc = (t.get("description") or "").split(" | ")[0]  # strip seed tag
                row = {
                    "name": t["name"], "desc": desc,
                    "amount": _money(dep or wd),
                    "amount_cents": _cents(dep or wd),
                    "direction": "in" if dep else "out",
                }
                unreconciled.append(row)
                if not str(t.get("reference_number") or "").startswith(CATEGORY_TAG):
                    uncategorized.append(row)

            # Close-readiness inputs: drafts that would be missed by the close, and an existing PCV.
            for key, dt in (("sales_invoices", "Sales Invoice"), ("purchase_invoices", "Purchase Invoice"),
                            ("journal_entries", "Journal Entry")):
                draft_counts[key] = len(_get_list(dt, ["name"], filters=[["company", "=", COMPANY],
                                                                         ["docstatus", "=", 0]]))
            existing_pcv = _existing_pcv(COMPANY, fiscal_year)
            checks_available = True
        except Exception as e:  # network / auth hiccup — surface, don't crash the page
            error = str(e)

    # Month-end close checklist: every item is computed from the data fetched above (no constants).
    # When ERPNext data is unavailable no check may pass — we cannot claim the books are ready.
    ok = checks_available
    checklist = [
        {"item": "No draft sales invoices", "done": ok and draft_counts["sales_invoices"] == 0,
         "count": draft_counts["sales_invoices"]},
        {"item": "No draft purchase invoices (bills)", "done": ok and draft_counts["purchase_invoices"] == 0,
         "count": draft_counts["purchase_invoices"]},
        {"item": "No draft journal entries", "done": ok and draft_counts["journal_entries"] == 0,
         "count": draft_counts["journal_entries"]},
        {"item": "Bank transactions categorized", "done": ok and len(uncategorized) == 0,
         "count": len(uncategorized)},
        {"item": "Bank transactions reconciled", "done": ok and len(unreconciled) == 0,
         "count": len(unreconciled)},
        {"item": "Period Closing Voucher posted", "done": ok and existing_pcv is not None,
         "count": 0 if existing_pcv else 1},
    ]
    done = sum(1 for c in checklist if c["done"])
    close_pct = round(100 * done / len(checklist))
    close_pending = not checklist[-1]["done"]
    # Blocking = every check except the PCV itself (the PCV is the thing being closed).
    blocking_checks = [{"item": c["item"], "count": c["count"]} for c in checklist[:-1] if not c["done"]]
    if not checks_available:
        blocking_checks = [{"item": "ERPNext data unavailable" + (f": {error}" if error else ""), "count": 0}]

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
        "blocking_checks": blocking_checks,
        "unreconciled": unreconciled,
        "close_pct": close_pct,
        "close_pending": close_pending,
        "counts": {"ar": len(ar_rows), "uncategorized": len(uncategorized),
                   "unreconciled": len(unreconciled), **{f"draft_{k}": v for k, v in draft_counts.items()}},
    }
    _CACHE.update(ts=now, data=data)
    return data


# ── agentic actions (deterministic ERPNext API work) ─────────────────────────
def categorize(blurb: Callable[[str], str | None] | None = None, approval=None) -> dict:
    """Categorize ONE uncategorized Bank Transaction. APPROVAL-GATED: without ``approval`` (a runtime-delivered
    ``_approval``) this only STAGES a preview of the proposed change and writes nothing to ERPNext.

    ERPNext's Bank Transaction has no account/category field and the existing client has no Payment Entry /
    allocation support, so the write is a ``reference_number`` tag (plus ``party_type`` on deposits). Output says
    so explicitly with ``mode: "tag_only"`` — it is NOT an accounting categorisation/allocation.
    `blurb` is an optional narration callback (the LLM one-liner lives in app.py).
    """
    data = fetch_activity(force=True)
    queue = data.get("uncategorized", [])
    if not queue:
        return {"status": "done", "action": "categorize", "mode": "tag_only", "summary":
                "No uncategorized transactions — nothing to do."}

    t = queue[0]
    category = "Revenue · Sales" if t["direction"] == "in" else "Materials · COGS"
    payload = {"reference_number": f"{CATEGORY_TAG}{category}"}
    if t["direction"] == "in":
        payload["party_type"] = "Customer"
    result = {"transaction": t["name"], "memo": t["desc"], "amount": t["amount"],
              "categorized_to": category}
    note = ("tag only: sets reference_number (and party_type on deposits); no account allocation or "
            "ledger posting is made")
    if not approval:
        return {"status": "pending_approval", "action": "categorize", "mode": "tag_only",
                "categorize_executed": False, "transaction_categorized": False, "requires": "human approval", "result": result,
                "proposed_changes": [{"doctype": "Bank Transaction", "name": t["name"], "set": payload}],
                "remaining_after": len(queue) - 1, "note": note,
                "summary": f"Proposed: tag {t['name']} ({t['desc']}) → {category} ({note}). "
                           "Staged for human approval; nothing was written to ERPNext."}

    put_ok = False
    try:
        with httpx.Client(timeout=12.0) as client:
            resp = client.put(
                f"{ERPNEXT_URL}/api/resource/Bank%20Transaction/{t['name']}",
                headers=_headers(), json=payload,
            )
            result["erpnext_status"] = resp.status_code
            put_ok = 200 <= resp.status_code < 300
            result["action"] = (
                f"tagged {t['name']} → {category}"
                if put_ok
                else f"update returned {resp.status_code}"
            )
    except Exception as e:
        result["action"] = f"error: {e}"
        result["error"] = str(e)
    if not put_ok:                      # never report "done" for a failed ERPNext write
        return {"status": "error", "action": "categorize", "mode": "tag_only", "result": result,
                "transaction_categorized": False,
                "error": result.get("error") or f"ERPNext returned HTTP {result.get('erpnext_status')}",
                "summary": f"Could NOT categorize {t['name']} ({t['desc']}): {result['action']}."}

    reasoning = blurb(
        "You are a bookkeeping agent for a wealth-management firm. In ONE sentence, "
        f"explain categorizing the bank transaction '{t['desc']}' ({t['amount']}) as "
        f"'{category}'. Be concrete and professional. Final answer only."
    ) if blurb else None
    out = {"status": "done", "action": "categorize", "mode": "tag_only", "categorize_executed": True,
           "transaction_categorized": True,
           "result": result, "note": note,
           "summary": f"Tagged 1 of {len(queue)} uncategorized bank transactions "
                      f"({t['desc']} → {category}); {note}."}
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
    deposits = [t for t in data.get("unreconciled", data.get("uncategorized", [])) if t["direction"] == "in"]
    match = None
    for inv in ar:
        for dep in deposits:
            if inv.get("amount_cents") is not None and inv.get("amount_cents") == dep.get("amount_cents"):
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


CLOSING_ACCOUNT = os.environ.get("BOOKS_CLOSING_ACCOUNT", "")   # Equity "Retained Earnings" head; looked up if unset


def _close_params(body: dict) -> dict:
    """The EXACT parameters the approval binds to: company, fiscal year, period window and the closing
    (retained-earnings) account. Derived from the proposal body + env, with a live ERPNext lookup for the
    Equity closing head when not configured. Same values are approved, executed and read back."""
    company = body.get("company") or COMPANY
    period_end = body.get("period_end") or time.strftime("%Y-%m-%d")
    period_start = body.get("period_start") or (period_end[:4] + "-01-01")
    fy = str(body.get("fiscal_year") or period_end[:4])
    acct = body.get("closing_account") or CLOSING_ACCOUNT
    if not acct:
        eq = _get_list("Account", ["name"], filters=[["company", "=", company], ["root_type", "=", "Equity"],
                                                      ["is_group", "=", 0]], limit=50)
        acct = next((a["name"] for a in eq if "retain" in a["name"].lower() or "reserve" in a["name"].lower()),
                    (eq[0]["name"] if eq else ""))
    return {"company": company, "fiscal_year": fy, "period_start_date": period_start,
            "period_end_date": period_end, "posting_date": period_end, "closing_account_head": acct}


def _existing_pcv(company: str, fy: str) -> dict | None:
    """The submitted Period Closing Voucher already closing this (company, fiscal year), if any — for
    idempotency + the 'reject inconsistent re-close' rule."""
    rows = _get_list("Period Closing Voucher", ["name", "fiscal_year", "company", "posting_date", "docstatus"],
                     filters=[["company", "=", company], ["fiscal_year", "=", fy]])
    return next((r for r in rows if int(r.get("docstatus") or 0) == 1), None)


def _existing_pcv_safe(params: dict) -> dict | None:
    try:
        return _existing_pcv(params["company"], params["fiscal_year"])
    except Exception:  # noqa: BLE001
        return None


def _execute_close(params: dict) -> dict:
    """Create + SUBMIT a real ERPNext Period Closing Voucher (docstatus=1 posts the closing entries to the
    GL and locks the period). Idempotent: if the period is already closed, returns that voucher instead of
    posting a second one; rejects a re-close with inconsistent period parameters."""
    if not (erpnext_connected() and ERPNEXT_API_KEY):
        return {"executed": False, "error": "erpnext not reachable"}
    if not params.get("closing_account_head"):
        return {"executed": False, "error": "no Equity closing (retained-earnings) account found/configured"}
    prior = _existing_pcv(params["company"], params["fiscal_year"])
    if prior is not None:
        if prior.get("posting_date") and params["posting_date"] and prior["posting_date"] != params["posting_date"]:
            return {"executed": False, "error": f"period {params['fiscal_year']} already closed on "
                    f"{prior['posting_date']} — refusing an inconsistent re-close", "voucher": prior["name"]}
        return {"executed": True, "voucher": prior["name"], "idempotent": True}   # already closed, same params
    payload = {"doctype": "Period Closing Voucher", "transaction_date": params["posting_date"],
               "posting_date": params["posting_date"], "company": params["company"],
               "fiscal_year": params["fiscal_year"], "closing_account_head": params["closing_account_head"],
               "remarks": f"Period close {params['fiscal_year']} via governed Mission"}
    with httpx.Client(timeout=20.0) as client:
        r = client.post(f"{ERPNEXT_URL}/api/resource/Period%20Closing%20Voucher", headers=_headers(), json=payload)
        if r.status_code >= 400:
            return {"executed": False, "error": f"create {r.status_code}: {r.text[:200]}"}
        name = ((r.json() or {}).get("data", {}) or {}).get("name")
        if not name:
            return {"executed": False, "error": "no voucher name returned"}
        sub = client.put(f"{ERPNEXT_URL}/api/resource/Period%20Closing%20Voucher/{name}",
                         headers=_headers(), json={"docstatus": 1})   # submit → posts to GL + locks period
        if sub.status_code >= 400:
            return {"executed": False, "error": f"submit {sub.status_code}: {sub.text[:200]}", "voucher": name}
        return {"executed": True, "voucher": name, "idempotent": False}


def verify_close(body: dict) -> dict:
    """N4 read-back: independently confirm the close actually took effect — the voucher is SUBMITTED, for the
    approved company + fiscal year, and the closing entry is in the GL. Reads from ERPNext, never trusts the
    create/submit response alone."""
    name = (body or {}).get("voucher")
    params = _close_params(body or {})
    if not name:
        return {"verified": False, "reason": "no voucher to verify"}
    pcv = _get_list("Period Closing Voucher", ["name", "fiscal_year", "company", "posting_date", "docstatus"],
                    filters=[["name", "=", name]])
    doc = pcv[0] if pcv else None
    if not doc or int(doc.get("docstatus") or 0) != 1:
        return {"verified": False, "reason": "voucher not found or not submitted", "voucher": name}
    if doc.get("company") != params["company"] or str(doc.get("fiscal_year")) != params["fiscal_year"]:
        return {"verified": False, "reason": "voucher company/fiscal-year differs from the approved close",
                "voucher": name}
    gl = _get_list("GL Entry", ["name"], filters=[["voucher_type", "=", "Period Closing Voucher"],
                                                  ["voucher_no", "=", name]], limit=1)
    return {"verified": bool(gl), "voucher": name, "submitted": True,
            "company": doc.get("company"), "fiscal_year": doc.get("fiscal_year"),
            "gl_entries_present": bool(gl),
            "reason": "" if gl else "voucher submitted but no closing GL entry read back"}


def _unreachable(action: str, exc: Exception, **flags) -> dict:
    """The structured result for an ERPNext outage: never an exception (a 500), always an error dict whose
    declared-output flags are False so the value-aware verifier fails the node cleanly."""
    return {"status": "error", "action": action, "error": "erpnext unreachable",
            "reason": f"{type(exc).__name__}: {exc}", **flags}


def close(body: dict | None = None, blurb: Callable[[str], str | None] | None = None) -> dict:
    """Month-end close (approval-gated in core; an ERPNext outage returns a structured error, never raises).
    See ``_close``."""
    try:
        return _close(body, blurb)
    except Exception as e:  # noqa: BLE001 — httpx connect/timeout/HTTP errors from any ERPNext read or write
        return _unreachable("close", e, close_staged=False, close_executed=False)


def _close(body: dict | None = None, blurb: Callable[[str], str | None] | None = None) -> dict:
    """Month-end close. On the ungoverned/pre-approval path it only STAGES a Period Closing Voucher proposal
    (pending_approval) — the approval binds to the EXACT company, fiscal year and period parameters shown.
    On a governed approval (resumed node carries ``_approval``) it EXECUTES: creates + SUBMITS the PCV (posts
    closing entries, locks the period), then READS BACK the submitted voucher + GL to verify (N3+N4).
    `blurb` narrates the staged proposal."""
    body = body or {}
    params = _close_params(body)
    data = fetch_activity(force=True)
    failing = data.get("blocking_checks", [])
    if failing and _existing_pcv_safe(params) is None:
        return {"status": "not_ready", "action": "close", "close_staged": False, "close_executed": False,
                "close_pct": data.get("close_pct", 0), "failing_checks": failing,
                "close_params": {k: params[k] for k in ("company", "fiscal_year", "period_end_date")},
                "summary": "Close NOT staged: " + "; ".join(
                    f"{c['item']} ({c['count']})" if c.get("count") else c["item"] for c in failing) + "."}
    if not body.get("_approval"):
        pct = data.get("close_pct", 0)
        blocker = "all checks pass"
        reasoning = blurb(
            "You are a bookkeeping agent for a wealth-management firm. In ONE sentence, summarize "
            f"that the June month-end close is {pct}% complete ({blocker}) and is staged for the "
            "owner's approval before the Period Closing Voucher is posted. Final answer only.") if blurb else None
        out = {
            "status": "pending_approval", "action": "close", "requires": "human approval",
            "close_staged": True, "close_pct": pct,
            "close_params": {k: params[k] for k in ("company", "fiscal_year", "period_end_date")},
            "summary": f"Month-end close is {pct}% complete ({blocker}). Period Closing Voucher for "
                       f"{params['company']} FY {params['fiscal_year']} (through {params['period_end_date']}) "
                       "is staged and awaiting human approval — the agent never closes the books on its own.",
        }
        if reasoning:
            out["reasoning"] = reasoning
        return out
    ex = _execute_close(params)
    if not ex.get("executed"):
        return {"status": "error", "action": "close", "close_staged": True, "close_executed": False,
                "error": ex.get("error"), "voucher": ex.get("voucher"),
                "summary": f"Close approved but NOT executed: {ex.get('error')}."}
    v = verify_close({**body, "voucher": ex["voucher"]})
    return {
        "status": "done" if v.get("verified") else "error",
        "action": "close", "close_staged": True, "close_executed": True,
        "voucher": ex["voucher"], "idempotent": ex.get("idempotent", False),
        "verified": v.get("verified"), "verification": v,
        "summary": (f"Period Closing Voucher {ex['voucher']} SUBMITTED + verified for {params['company']} "
                    f"FY {params['fiscal_year']}." if v.get("verified") else
                    f"PCV {ex['voucher']} submitted but read-back verification FAILED: {v.get('reason')}."),
    }


# ── onboarding: record subscription revenue (+ its compensating undo) ─────────
def _revenue_accounts() -> tuple[str, str]:
    """Pick a (cash/bank, income) account pair from the live chart of accounts.

    Falls back to Meridian's seeded account names if the lookup returns nothing, so the
    Journal Entry payload is always structurally valid (debits/credits both reference an
    account). Same `_get_list` REST convention as the KPI code above.
    """
    accts = _get_list(
        "Account",
        fields=["name", "root_type", "account_type"],
        filters=[["company", "=", COMPANY], ["is_group", "=", 0]],
    )
    income_acc = next((a["name"] for a in accts if a.get("root_type") == "Income"),
                      "Sales - MWM")
    cash_acc = next((a["name"] for a in accts
                     if a.get("account_type") in ("Bank", "Cash")), "Bank - MWM")
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
    if not body.get("_approval"):             # gate in CORE: every caller (operator, route, console) is covered
        return pending_approval("books.record_revenue", amount=body.get("amount"), revenue_recorded=False,
                                subscription=subscription)
    try:
        amount = parse_revenue_amount(body.get("amount"))
    except ValueError as e:
        return {"status": "error", "action": "record_revenue", "revenue_recorded": False,
                "error": str(e)}
    cur = body.get("currency")
    if cur is not None and (not isinstance(cur, str) or cur.strip().upper() != COMPANY_CURRENCY):
        return {"status": "error", "action": "record_revenue", "revenue_recorded": False,
                "error": f"currency {cur!r} does not match the ledger currency {COMPANY_CURRENCY}"}
    try:
        cash_acc, income_acc = _revenue_accounts()
    except Exception as e:  # noqa: BLE001 — ERPNext unreachable: structured error, not a 500
        return _unreachable("record_revenue", e, revenue_recorded=False, entry=None)
    idem = body.get("idempotency_key") or (
        f"{body['_mission']}:{body['_node']}" if body.get("_mission") and body.get("_node") else "")
    if not idem:
        return _execute_record_revenue(body, subscription, amount, cash_acc, income_acc, "", "", None)
    # scope the key to tenant + company + operation so keys cannot collide across tenants
    token = "idem:" + hashlib.sha256(
        f"{TENANT}|{body.get('_tenant') or ''}|{COMPANY}|record_revenue|{idem}".encode()).hexdigest()[:24]
    with _token_lock(token):
        return _record_revenue_post(body, subscription, amount, cash_acc, income_acc, token,
                                    _request_hash(amount, COMPANY_CURRENCY, cash_acc, income_acc, subscription))


_TOKEN_LOCKS: dict = {}
_TOKEN_LOCKS_GUARD = threading.Lock()


def _token_lock(token: str):
    with _TOKEN_LOCKS_GUARD:
        return _TOKEN_LOCKS.setdefault(token, threading.Lock())


def _request_hash(amount, currency, cash_acc, income_acc, subscription) -> str:
    """Canonical hash of the request payload (amount, currency, accounts, memo) stored with the token."""
    canon = _json.dumps({"amount": str(amount), "currency": currency, "debit": cash_acc, "credit": income_acc,
                         "memo": subscription}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _draft_matches(name, amount, cash_acc, income_acc) -> bool:
    """A resumed draft must carry exactly the requested amount and accounts before it is submitted."""
    try:
        with httpx.Client(timeout=12.0) as client:
            r = client.get(f"{ERPNEXT_URL}/api/resource/Journal%20Entry/{quote(str(name), safe='')}",
                           headers=_headers())
            doc = (r.json() or {}).get("data") or {}
        rows = doc.get("accounts") or []
        debit = sum((Decimal(str(a.get("debit_in_account_currency") or 0)) for a in rows), Decimal(0))
        credit = sum((Decimal(str(a.get("credit_in_account_currency") or 0)) for a in rows), Decimal(0))
        return (len(rows) == 2 and debit == amount and credit == amount
                and {a.get("account") for a in rows} == {cash_acc, income_acc})
    except Exception:  # noqa: BLE001
        return False


def _record_revenue_post(body, subscription, amount, cash_acc, income_acc, token, req_hash) -> dict:
    if not body.get("_approval"):             # defence in depth: never look up or write without an approval
        return pending_approval("books.record_revenue", revenue_recorded=False)
    existing = None
    if token:                                  # look up before create: a re-run must not post a second entry
        try:
            rows = _get_list("Journal Entry", fields=["name", "docstatus", "user_remark"],
                             filters=[["user_remark", "like", f"%{token}%"], ["docstatus", "!=", 2]])
        except Exception as e:  # noqa: BLE001
            return _unreachable("record_revenue", e, revenue_recorded=False, entry=None)
        existing = rows[0] if rows else None
        if existing:
            if f"[req:{req_hash}]" not in str(existing.get("user_remark") or ""):
                return {"status": "error", "action": "record_revenue", "executed": False,
                        "revenue_recorded": False, "error_type": "idempotency_conflict",
                        "error": "this idempotency key was already used with a different request; nothing written",
                        "retained": {"doctype": "Journal Entry", "name": existing.get("name")}}
            if existing.get("docstatus") == 1:
                return {"status": "done", "action": "record_revenue", "revenue_recorded": True,
                        "entry": existing.get("name"), "deduplicated": True, "currency": COMPANY_CURRENCY,
                        "summary": f"Journal Entry {existing.get('name')} already posted for this request; nothing new written."}
            if not _draft_matches(existing.get("name"), amount, cash_acc, income_acc):
                return {"status": "error", "action": "record_revenue", "executed": False,
                        "revenue_recorded": False, "error_type": "idempotency_conflict",
                        "error": "the retained draft does not match the requested payload; not submitted",
                        "retained": {"doctype": "Journal Entry", "name": existing.get("name"), "docstatus": 0}}
    return _execute_record_revenue(body, subscription, amount, cash_acc, income_acc, token, req_hash, existing)


def _execute_record_revenue(body, subscription, amount, cash_acc, income_acc, token, req_hash, existing) -> dict:
    payload = {
        "doctype": "Journal Entry",
        "voucher_type": "Journal Entry",
        "posting_date": time.strftime("%Y-%m-%d"),
        "company": COMPANY,
        "user_remark": f"Onboarding subscription revenue: {subscription}" + (f" [{token}][req:{req_hash}]" if token else ""),
        "accounts": [
            {"account": cash_acc,
             "debit_in_account_currency": float(amount), "credit_in_account_currency": 0},
            {"account": income_acc,
             "debit_in_account_currency": 0, "credit_in_account_currency": float(amount)},
        ],
    }
    entry = None
    erpnext_status = None
    submitted = False
    try:
        with httpx.Client(timeout=12.0) as client:
            if existing:                       # resume a draft left by an earlier attempt
                entry = existing.get("name")
            else:
                resp = client.post(
                    f"{ERPNEXT_URL}/api/resource/Journal%20Entry",
                    headers=_headers(), json=payload,
                )
                erpnext_status = resp.status_code
                doc = (resp.json() or {}).get("data", {}) if 200 <= resp.status_code < 300 else {}
                entry = doc.get("name")
                if entry and token:            # detect a concurrent duplicate that slipped past the lookup
                    dup = client.get(f"{ERPNEXT_URL}/api/resource/Journal%20Entry", headers=_headers(), params={
                        "fields": _json.dumps(["name"]), "limit_page_length": "0",
                        "filters": _json.dumps([["user_remark", "like", f"%{token}%"], ["docstatus", "!=", 2]])})
                    names = [r.get("name") for r in ((dup.json() or {}).get("data") or [])]
                    if len([n for n in names if n]) > 1:
                        return {"status": "error", "action": "record_revenue", "executed": False,
                                "revenue_recorded": False, "error_type": "duplicate_entry",
                                "error": "more than one entry exists for this request; not submitted",
                                "retained": {"doctype": "Journal Entry", "names": names}}
            # SUBMIT it (docstatus=1) so it actually POSTS TO THE GENERAL LEDGER. A Journal Entry created
            # via the resource API is a DRAFT (docstatus=0) and records NOTHING in the GL — so without this
            # the revenue was never booked, and the saga's cancel (docstatus=2) can't cancel a draft either.
            # Same PUT-docstatus convention reverse_entry uses.
            if entry:
                sub = client.put(
                    f"{ERPNEXT_URL}/api/resource/Journal%20Entry/{quote(str(entry), safe='')}",
                    headers=_headers(), json={"docstatus": 1},   # 1 = Submitted → posts to the GL
                )
                erpnext_status = sub.status_code
                submitted = 200 <= sub.status_code < 300
    except Exception as e:  # network / auth hiccup — surface, don't crash the saga
        erpnext_status = f"error: {e}"

    if not submitted:
        # Not posted: claim nothing. A draft that was created is RETAINED (never auto-deleted); its id is
        # reported so a human or a retry with the same idempotency key can finish it.
        return {"status": "error", "action": "record_revenue", "executed": False, "revenue_recorded": False,
                "retained": ({"doctype": "Journal Entry", "name": entry, "docstatus": 0} if entry else None),
                "erpnext_status": erpnext_status,
                "error": "journal entry was not submitted to the ledger",
                "summary": f"Could NOT post subscription revenue for '{subscription}' "
                           f"(draft {entry or '(none created)'} retained, not submitted to the GL)."}
    return {
        "status": "done",
        "action": "record_revenue",
        "revenue_recorded": submitted,            # true only if the JE was actually SUBMITTED (posted)
        "entry": entry,
        "currency": COMPANY_CURRENCY,
        "erpnext_status": erpnext_status,
        "summary": (f"Recorded {_money(float(amount))} subscription revenue for '{subscription}' "
                    f"as Journal Entry {entry or '(pending)'} "
                    f"(debit {cash_acc} / credit {income_acc})."
                    if submitted else
                    f"Could NOT post subscription revenue for '{subscription}' "
                    f"(entry {entry or '(not created)'} not submitted to the GL)."),
    }


def reverse_entry(body: dict | None = None) -> dict:
    """Compensating action (saga undo): reverse the revenue Journal Entry.

    Cancels the voucher created by `record_revenue` — a submit-state PUT (docstatus=2)
    against `.../Journal Entry/{name}`, the ERPNext cancel path. `body` carries the
    `entry` (or `name`) returned by record_revenue. Idempotent: cancelling an already
    cancelled voucher is a no-op on ERPNext's side.
    """
    body = body or {}
    fwd = body.get("result") if isinstance(body.get("result"), dict) else {}   # saga undo nests the forward result
    name = fwd.get("entry") or body.get("entry") or body.get("name")
    if not body.get("_approval"):             # gate in CORE
        return pending_approval("books.reverse_entry", entry=name, revenue_reversed=False)
    if not _valid_doc_name(name):             # never guess a target: refuse when the entry id is missing/invalid
        return {"status": "error", "action": "reverse_entry", "reversed": False, "revenue_reversed": False,
                "entry": None, "error": "no valid journal entry id supplied; nothing was reversed",
                "summary": "Could NOT reverse: no valid Journal Entry id supplied."}
    erpnext_status = None
    reversed_ok = False
    if name:
        try:
            with httpx.Client(timeout=12.0) as client:
                url = f"{ERPNEXT_URL}/api/resource/Journal%20Entry/{quote(name, safe='')}"
                resp = client.put(url, headers=_headers(), json={"docstatus": 2})  # 2 = Cancelled
                erpnext_status = resp.status_code
                if 200 <= resp.status_code < 300:
                    # Claim `reversed` only if the document ACTUALLY transitioned to Cancelled: read the
                    # docstatus off the PUT response, else re-read the doc.
                    def _ds(r):
                        try:
                            return ((r.json() or {}).get("data") or {}).get("docstatus")
                        except Exception:  # noqa: BLE001
                            return None
                    ds = _ds(resp)
                    if ds is None:
                        ds = _ds(client.get(url, headers=_headers()))
                    reversed_ok = ds == 2
        except Exception as e:  # noqa: BLE001
            erpnext_status = f"error: {e}"

    return {
        "status": "done" if reversed_ok else "error",
        "action": "reverse_entry",
        "reversed": reversed_ok,
        "revenue_reversed": reversed_ok,
        "entry": name,
        "erpnext_status": erpnext_status,
        "summary": (f"Reversed revenue Journal Entry {name} — subscription revenue backed out."
                    if reversed_ok else
                    f"Could NOT reverse Journal Entry {name or '(none supplied)'} "
                    f"(ERPNext status {erpnext_status}; document not confirmed Cancelled)."),
    }
