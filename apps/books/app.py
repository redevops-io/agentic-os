"""agentic-books — agent layer + dashboard over a real ERPNext core (the 9th module).

Same pattern as agentic-billing (the reference), pointed at the running self-hosted
ERPNext instead of Lago:

  * an agent layer that reads REAL ERPNext data over its REST API, and
  * a QuickBooks/Xero-style MD3 dashboard (same design tokens as
    deploy/module_service.py) rendered from that live data — no mock data.

Endpoints:
  GET  /health        -> {"status","core":"erpnext","connected": <bool from a cheap call>}
  GET  /api/activity  -> live KPIs (cash, net income, A/R, A/P) + uncategorized txns + close
  GET  /              -> MD3 books dashboard rendered from the live data
  POST /agent/run     -> {"action":"categorize"|"reconcile"|"close"|"record_revenue"|"reverse_entry"}
                         every write is approval-gated IN core (returns pending_approval unless the route
                         proved operator Basic auth).

Config (env; seed.py writes agents/books/.env automatically):
  ERPNEXT_URL         REST base, default http://localhost:8092
  ERPNEXT_API_KEY     Administrator API key
  ERPNEXT_API_SECRET  Administrator API secret  (Authorization: token key:secret)
  ERPNEXT_FRONT_URL   ERPNext UI link for the "Open in ERPNext" button
  COMPANY             the books company, default "Meridian Wealth Management"
  PORT                uvicorn port, default 8209
  REDEVOPS_LLM_BASE_URL  OPTIONAL in-boundary model for /agent/run narration (agentic_os.app.llm.governed_text,
                      CUSTOMER_CONFIDENTIAL, fail-closed; no external fallback)
  OPERATOR_BASIC_AUTH ``user:pass`` -- a request carrying these Basic credentials is the operator's approval for
                      /agent/run actions; everything else only gets a staged preview
"""
from __future__ import annotations

import html
import os

# ── enterprise permissions plane — gate tool/data access by (app, user) ──
# No-op without the context_runtime_enterprise package or CR_PERMISSIONS=1 (open-core default).
try:
    from context_runtime_enterprise.apps import bootstrap as _cr_bootstrap
    _req_principal = _cr_bootstrap("books")
except Exception:  # noqa: BLE001
    def _req_principal(request=None):
        return None

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# --- config ------------------------------------------------------------------
# The ERPNext REST client, KPIs and agentic actions live in core.py (pure — no web /
# context-runtime deps) so the Mission Runtime operator can invoke them and they can be
# tested against a fake ERPNext. Import config + core helpers from there; core loads .env.
from . import core
from .gate import operator_approval
from .core import (  # noqa: E402
    ERPNEXT_URL, ERPNEXT_API_KEY, ERPNEXT_API_SECRET, ERPNEXT_FRONT_URL, COMPANY,
    TENANT, SUBTITLE, APPROVAL_REQUIRED,
    _headers, erpnext_connected, _get_list, _money, _pl_and_cash, fetch_activity,
)

PORT = int(os.environ.get("PORT", "8209"))

def _strip_approval(obj):
    """Drop every caller-supplied ``_approval*`` / ``__approval*`` key (any case), recursively.

    ``_approval`` is the Mission runtime's marker for a human-approved resume and is injected IN-PROCESS by the
    operator. A raw HTTP body must never be able to set it, or a caller could skip the staging/approval gate.
    """
    if isinstance(obj, dict):
        return {k: _strip_approval(v) for k, v in obj.items()
                if not (isinstance(k, str) and k.lstrip("_").lower().startswith("approval"))}
    if isinstance(obj, list):
        return [_strip_approval(v) for v in obj]
    return obj


app = FastAPI(title="agentic-books (Meridian Wealth Management · core: ERPNext)")

from agentic_os.app_kit import route_authz as _route_authz  # noqa: E402
from .route_policy import APP as _APP, ROUTE_POLICIES as _ROUTE_POLICIES  # noqa: E402
_route_authz.install(app, _APP, _ROUTE_POLICIES)   # one enforcement layer for every write route


# --- MD3 styling (BASE_CSS reused verbatim from deploy/module_service.py) -----
BASE_CSS = """
:root{
  --surface-dim:#0e0e11; --surface:#131316; --surface-bright:#393a3d;
  --surface-container-lowest:#0d0e10; --surface-container-low:#1b1b1f;
  --surface-container:#1f1f23; --surface-container-high:#2a2a2e; --surface-container-highest:#353539;
  --on-surface:#e4e2e6; --on-surface-variant:#c7c5ca; --on-surface-muted:#918f96;
  --outline:#938f99; --outline-variant:#2f2f33;
  --primary:#4fd1c5; --on-primary:#00201c; --primary-container:#00504a; --on-primary-container:#a8f0e6;
  --secondary:#f5b544; --on-secondary:#3d2e00; --secondary-container:#5c4500;
  --success:#5bd98a; --success-container:#0f3d22; --warning:#f5b544; --warning-container:#4a3500;
  --danger:#f2544f; --danger-container:#5c1512; --info:#5aa9f0; --info-container:#103a5c;
  --sp-1:4px;--sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-5:24px;--sp-6:32px;--sp-7:40px;--sp-8:48px;
  --radius-sm:8px;--radius-md:12px;--radius-lg:16px;--radius-xl:28px;--radius-pill:999px;
  --shadow-1:0 1px 2px rgba(0,0,0,.45);--shadow-2:0 2px 6px rgba(0,0,0,.5);
  --font-sans:"Roboto",system-ui,-apple-system,"Segoe UI",sans-serif;
  --font-mono:"Roboto Mono",ui-monospace,"SF Mono",monospace;
}
*{box-sizing:border-box}
.display-l{font:400 57px/64px var(--font-sans);letter-spacing:-.25px}
.headline-m{font:400 28px/36px var(--font-sans)} .headline-s{font:400 24px/32px var(--font-sans)}
.title-l{font:400 22px/28px var(--font-sans)} .title-m{font:500 16px/24px var(--font-sans);letter-spacing:.15px}
.title-s{font:500 14px/20px var(--font-sans)} .body-m{font:400 14px/20px var(--font-sans)}
.body-s{font:400 12px/16px var(--font-sans)} .label-m{font:500 12px/16px var(--font-sans);letter-spacing:.5px}
.page{background:var(--surface);color:var(--on-surface);font-family:var(--font-sans);padding:var(--sp-5);margin:0}
.shell{max-width:1440px;margin-inline:auto;display:flex;flex-direction:column;gap:var(--sp-5)}
.grid{display:grid;gap:var(--sp-4);grid-template-columns:repeat(12,1fr)}
.kpi-row{display:grid;gap:var(--sp-4);grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.col-3{grid-column:span 3}.col-4{grid-column:span 4}.col-6{grid-column:span 6}.col-8{grid-column:span 8}.col-12{grid-column:span 12}
@media(max-width:839px){[class^="col-"]{grid-column:span 12}}
.card{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-4)}
.card__head{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3)}
.card__title{font:500 16px/24px var(--font-sans);letter-spacing:.15px;color:var(--on-surface);margin:0}
.tile{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-4) var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-1)}
.tile__label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--on-surface-muted)}
.tile__value{font:500 32px/40px var(--font-mono);color:var(--on-surface);font-feature-settings:"tnum"}
.tile__delta{font:500 12px/16px var(--font-sans);color:var(--on-surface-variant)} .tile__delta--up{color:var(--success)} .tile__delta--down{color:var(--danger)}
.pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:var(--radius-pill);font:500 12px/1 var(--font-sans)}
.pill--success{background:var(--success-container);color:var(--success)}.pill--warn{background:var(--warning-container);color:var(--warning)}
.pill--danger{background:var(--danger-container);color:var(--danger)}.pill--info{background:var(--info-container);color:var(--info)}
.pill--neutral{background:var(--surface-container-highest);color:var(--on-surface-variant)}
.pill__dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.table{width:100%;border-collapse:collapse;font-size:14px}
.table th{text-align:left;color:var(--on-surface-muted);font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;padding:var(--sp-3) var(--sp-4);border-bottom:1px solid var(--outline-variant)}
.table td{padding:var(--sp-3) var(--sp-4);color:var(--on-surface);border-bottom:1px solid var(--outline-variant)}
.table td.num{text-align:right;font-family:var(--font-mono);font-feature-settings:"tnum"}
.table tbody tr:last-child td{border-bottom:none}
.table tbody tr:hover{background:rgba(228,226,230,.08)}
.banner{display:flex;align-items:center;gap:var(--sp-4);padding:var(--sp-4) var(--sp-5);border-radius:var(--radius-md);border-left:4px solid var(--warning);background:var(--warning-container);color:var(--on-surface)}
.bar{height:8px;border-radius:var(--radius-pill);background:var(--surface-container-highest);overflow:hidden}
.bar>span{display:block;height:100%;background:var(--primary)}
"""

PAGE_CSS = """
a{color:var(--primary);text-decoration:none}
.appbar{background:var(--surface-container-low);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5) var(--sp-5)}
.appbar__row{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap}
.appbar h1{margin:0;font:400 28px/36px var(--font-sans);color:var(--on-surface)}
.appbar__tenant{margin-top:var(--sp-3);color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}
.appbar__tenant b{color:var(--on-surface)}
.appbar__sub{margin-top:var(--sp-2);color:var(--on-surface-muted);font:400 14px/20px var(--font-sans);max-width:820px}
.spacer{flex:1}
.btn{display:inline-flex;align-items:center;gap:6px;height:36px;padding:0 16px;border-radius:var(--radius-pill);background:var(--primary-container);color:var(--on-primary-container);font:500 14px/1 var(--font-sans);border:1px solid var(--primary-container)}
.btn:hover{filter:brightness(1.1)}
.section-label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--primary);display:flex;align-items:center;gap:var(--sp-3);margin:0}
.section-label::after{content:"";flex:1;height:1px;background:var(--outline-variant)}
.barlist{display:flex;flex-direction:column;gap:var(--sp-4)}
.barlist__row{display:grid;grid-template-columns:170px 1fr 96px;align-items:center;gap:var(--sp-4)}
.barlist__label{color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}
.barlist__pct{text-align:right;font-family:var(--font-mono);font-feature-settings:"tnum";font-size:13px;color:var(--on-surface-variant)}
.checklist{display:flex;flex-direction:column;gap:var(--sp-3)}
.checkrow{display:flex;align-items:center;gap:var(--sp-3);font:400 14px/20px var(--font-sans);color:var(--on-surface-variant)}
.checkbox{width:18px;height:18px;border-radius:var(--radius-sm);border:1.5px solid var(--outline);display:inline-flex;align-items:center;justify-content:center;font-size:12px;color:var(--on-primary)}
.checkbox--done{background:var(--success);border-color:var(--success);color:#06251a}
.progresswrap{display:flex;flex-direction:column;gap:var(--sp-2)}
.footer{color:var(--on-surface-muted);font:400 12px/16px var(--font-sans);text-align:center;padding-top:var(--sp-2)}
"""

FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Roboto:wght@400;500&family=Roboto+Mono:wght@400;500&display=swap">'
)


def _esc(v) -> str:
    return html.escape(str(v))


def _kpi_tiles(kpis: list[dict]) -> str:
    cells = ""
    for k in kpis:
        cells += (
            "<div class='tile'>"
            f"<div class='tile__label'>{_esc(k['label'])}</div>"
            f"<div class='tile__value'>{_esc(k['value'])}</div>"
            f"<div class='tile__delta'>{_esc(k['note'])}</div>"
            "</div>"
        )
    return f"<section class='kpi-row'>{cells}</section>"


def _approval_banner(data: dict) -> str:
    """Month-end close is staged + awaiting approval (the approval-gated action)."""
    if not data.get("close_pending"):
        return ""
    pct = data.get("close_pct", 0)
    blocking = data.get("blocking_checks", [])
    note = (" Not ready: " + "; ".join(_esc(c["item"]) for c in blocking) + "."
            if blocking else " All checks pass — ready to post on your approval.")
    return (
        "<div class='banner'>"
        f"<span class='pill pill--warn'><span class='pill__dot'></span>close {pct}%</span>"
        "<span class='label-m' style='text-transform:uppercase;color:var(--warning)'>close</span>"
        f"<span class='body-m'>Month-end close is staged ({pct}% complete) and awaiting your "
        f"approval to post the Period Closing Voucher.{note} The agent never closes the "
        "books without a human sign-off.</span>"
        "</div>"
    )


def _pl_bars(data: dict) -> str:
    """Income-vs-expense P&L meter from the live ledger numbers."""
    pl = data["pl"]
    income = max(pl["income"], 1)
    rows = [
        ("Revenue (income)", pl["income"]),
        ("Expenses (COGS + payroll + OH)", pl["expense"]),
        ("Net profit", max(pl["net"], 0)),
    ]
    body = ""
    for label, val in rows:
        pct = int(round(100 * val / income))
        body += (
            "<div class='barlist__row'>"
            f"<div class='barlist__label'>{_esc(label)}</div>"
            f"<div class='bar'><span style='width:{min(pct,100)}%'></span></div>"
            f"<div class='barlist__pct'>${val:,.0f}</div>"
            "</div>"
        )
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>P&amp;L this month (live)</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from ERPNext</span></div>"
        f"<div class='barlist'>{body}</div>"
        "</div>"
    )


def _uncategorized_card(data: dict) -> str:
    rows = ""
    for t in data["uncategorized"][:8]:
        tone = "pill--success" if t["direction"] == "in" else "pill--warn"
        sign = "+" if t["direction"] == "in" else "−"
        rows += (
            "<tr>"
            f"<td>{_esc(t['desc'])}</td>"
            f"<td class='num'>{sign}{_esc(t['amount'])}</td>"
            f"<td><span class='pill {tone}'>uncategorized</span></td>"
            "</tr>"
        )
    if not rows:
        rows = "<tr><td colspan='3'>No uncategorized transactions — queue is clear.</td></tr>"
    n = len(data["uncategorized"])
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Uncategorized transactions</h2>"
        f"<span class='pill pill--warn'><span class='pill__dot'></span>{n} in queue</span></div>"
        "<table class='table'><thead><tr><th>Bank memo</th><th>Amount</th><th>State</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "</div>"
    )


def _ar_card(data: dict) -> str:
    rows = ""
    for i in data["ar_rows"]:
        rows += (
            "<tr>"
            f"<td>{_esc(i['number'])}</td>"
            f"<td>{_esc(i['customer'])}</td>"
            f"<td class='num'>{_esc(i['amount'])}</td>"
            f"<td><span class='pill pill--warn'>{_esc(i['status'])}</span></td>"
            "</tr>"
        )
    if not rows:
        rows = "<tr><td colspan='4'>No outstanding receivables.</td></tr>"
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Accounts receivable (open)</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from ERPNext</span></div>"
        "<table class='table'><thead><tr><th>Invoice</th><th>Customer</th><th>Outstanding</th><th>Status</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "</div>"
    )


def _close_card(data: dict) -> str:
    pct = data["close_pct"]
    rows = ""
    for c in data["checklist"]:
        mark = "✓" if c["done"] else ""
        cls = "checkbox--done" if c["done"] else ""
        rows += (
            "<div class='checkrow'>"
            f"<span class='checkbox {cls}'>{mark}</span>"
            f"<span>{_esc(c['item'])}</span>"
            "</div>"
        )
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Month-end close</h2>"
        f"<span class='pill pill--warn'><span class='pill__dot'></span>{pct}% complete</span></div>"
        "<div class='progresswrap'>"
        f"<div class='bar'><span style='width:{pct}%'></span></div>"
        "</div>"
        f"<div class='checklist'>{rows}</div>"
        "</div>"
    )


def render(data: dict) -> str:
    connected = data["connected"]
    conn_txt = "core: ERPNext connected" if connected else "core: ERPNext UNREACHABLE"
    conn_cls = "pill--success" if connected else "pill--danger"
    status_pill = (
        f"<span class='pill {conn_cls}'><span class='pill__dot'></span>agent active · {_esc(conn_txt)}</span>"
    )
    live_badge = "<span class='pill pill--info'><span class='pill__dot'></span>data: live from ERPNext</span>"
    open_btn = f"<a class='btn' href='{_esc(data['front_url'])}' target='_blank' rel='noopener'>Open in ERPNext ↗</a>"

    body = (
        _approval_banner(data)
        + _kpi_tiles(data["kpis"])
        + "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Profit &amp; loss</div>"
        "<div class='grid'>"
        f"<div class='col-6'>{_pl_bars(data)}</div>"
        f"<div class='col-6'>{_ar_card(data)}</div>"
        "</div></section>"
        + "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Categorize &amp; close</div>"
        "<div class='grid'>"
        f"<div class='col-6'>{_uncategorized_card(data)}</div>"
        f"<div class='col-6'>{_close_card(data)}</div>"
        "</div></section>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentic Books — {_esc(TENANT)}</title>
{FONT_LINK}
<style>{BASE_CSS}{PAGE_CSS}</style>
</head>
<body class="page">
<div class="shell">
  <header class="appbar">
    <div class="appbar__row">
      <h1>Agentic Books</h1>
      {status_pill}
      {live_badge}
      <span class="spacer"></span>
      {open_btn}
    </div>
    <div class="appbar__tenant"><b>{_esc(TENANT)}</b> · core: ERPNext (open-source accounting)</div>
    <div class="appbar__sub">{_esc(SUBTITLE)}</div>
  </header>
  {body}
  <footer class="footer">agentic-books · live activity for {_esc(TENANT)} ·
    <a href="/api/activity">/api/activity</a> · agent + human, on a real ERPNext core · redevops.io Agentic Business OS</footer>
</div>
</body>
</html>"""


# --- optional LLM reasoning blurb (governed; works without any model) --------
def _llm_blurb(prompt: str) -> str | None:
    """A one-line narration from the GOVERNED in-boundary model, or None.

    N7: the prompt carries customer/ledger data, so it is classified CUSTOMER_CONFIDENTIAL and routed by
    ``agentic_os.app.llm.governed_text`` -- fail closed (None) when the router refuses or no in-boundary
    endpoint is configured; there is no direct-HTTP fallback. The model only narrates."""
    from .assistant import governed_answer
    try:
        from agentic_os.governance.classification import DataClassification
    except Exception:  # noqa: BLE001
        return None
    return governed_answer(prompt, (DataClassification.CUSTOMER_CONFIDENTIAL,), max_tokens=220, temperature=0.3)


def _with_approval(body: dict, approval) -> dict:
    """The core call's body: every caller-supplied ``_approval*`` key is stripped, then the route's
    server-side marker -- if any -- is set."""
    clean = _strip_approval(body or {})
    return {**clean, "_approval": approval} if approval else clean


# --- agentic actions (thin wrappers over core; the approval gate lives IN core) ---
# ``approval`` is None for every caller except a route that proved operator Basic auth (see gate.py).
def _categorize(approval=None) -> dict:
    """Categorize the next uncategorized bank transaction (staged preview unless approved)."""
    return core.categorize(blurb=_llm_blurb, approval=approval)


def _reconcile() -> dict:
    """Stage a deposit↔invoice match (bank reconciliation, read-only), with optional narration."""
    return core.reconcile(blurb=_llm_blurb)


def _close(approval=None) -> dict:
    """Month-end close -- staged unless approved; an ERPNext outage returns a structured error."""
    return core.close({"_approval": approval} if approval else {}, blurb=_llm_blurb)


def _record_revenue(body: dict, approval=None) -> dict:
    """Onboarding: record subscription revenue as a real ERPNext Journal Entry (staged unless approved)."""
    return core.record_revenue(_with_approval(body, approval))


def _reverse_entry(body: dict, approval=None) -> dict:
    """Compensating saga undo: cancel the revenue Journal Entry (staged unless approved)."""
    return core.reverse_entry(_with_approval(body, approval))


#: every /agent/run action -> handler(body, approval). The route is the ONLY place the operator marker enters.
ACTIONS = {
    "categorize": lambda body, approval: _categorize(approval),
    "reconcile": lambda body, approval: _reconcile(),
    "close": lambda body, approval: _close(approval),
    "record_revenue": _record_revenue,
    "reverse_entry": _reverse_entry,
}


# ──────── conversational agent · Context Runtime (LLM) + redevops-rag (grounding) + agent-harness (tools) ────────
from .assistant import Assistant as _Assistant, Tool as _Tool

_BOOKS_PRIMER = """A CUSTOMER is anyone you sell to; a SUPPLIER (vendor) is anyone you buy from. Create a customer under Accounts → Customer → New (name, group, territory); create a supplier under Accounts → Supplier → New. An ITEM is a product or service you sell or buy; add one under Stock → Item → New, and set its default income/expense accounts.

A SALES INVOICE bills a customer for goods or services. Create it under Accounts → Sales Invoice → New: pick the customer, add items with rate and quantity, then Save and Submit. Once submitted it posts to the ledger and its outstanding_amount becomes ACCOUNTS RECEIVABLE (A/R) — money customers owe you.

A PURCHASE INVOICE (bill) records what you owe a supplier. Create it under Accounts → Purchase Invoice → New: pick the supplier, add items, Save and Submit. Its outstanding amount becomes ACCOUNTS PAYABLE (A/P) — money you owe.

To record a CUSTOMER PAYMENT: Accounts → Payment Entry → New → Payment Type "Receive", Party Type "Customer", pick the customer, set the paid amount and the bank/cash account, then under References pull in the Sales Invoice it settles, Save and Submit. This clears (or reduces) that invoice's outstanding balance. To pay a SUPPLIER, use Payment Type "Pay", Party Type "Supplier", against a Purchase Invoice.

A JOURNAL ENTRY is a manual double-entry posting (debits must equal credits) for anything that isn't a standard invoice or payment — accruals, depreciation, payroll, corrections. Create it under Accounts → Journal Entry → New, add account rows with debit/credit amounts, Save and Submit.

The CHART OF ACCOUNTS (Accounts → Chart of Accounts) is the tree of all ledger accounts, grouped by ROOT TYPE: Asset, Liability, Income, Expense, and Equity. Income accounts carry a credit nature; expense accounts a debit nature. NET INCOME (profit & loss) is total Income minus total Expense over a period.

A FISCAL YEAR defines your accounting period (e.g. 1 Jan–31 Dec). MONTH-END CLOSE means all invoices and bills are booked, bank transactions are categorized and reconciled, and finally a PERIOD CLOSING VOUCHER is posted to lock the period and roll profit into retained earnings. Posting that voucher locks the books, so the agent always stages it for a human to approve — it never closes on its own.

BANK RECONCILIATION matches bank transactions (deposits and withdrawals) to invoices and payments so the books agree with the bank statement. Use Accounts → Bank Reconciliation Tool. An uncategorized transaction is one not yet matched to an account."""


def _t_summary(_a: dict) -> dict:
    d = fetch_activity()
    k = {x["label"]: x["value"] for x in d["kpis"]}
    c = d["counts"]
    pl = d["pl"]
    return {"text": f"Cash position {k.get('Cash position')}, net income {k.get('Net income (P&L)')} "
            f"(revenue ${pl['income']:,.0f} − expense ${pl['expense']:,.0f}). "
            f"A/R outstanding {k.get('A/R outstanding')} across {c['ar']} open invoice(s); "
            f"A/P outstanding {k.get('A/P outstanding')}. "
            f"{c['uncategorized']} bank txn(s) still uncategorized.",
            "data": d["kpis"]}


def _t_unpaid(_a: dict) -> dict:
    ar = fetch_activity()["ar_rows"]
    if not ar:
        return {"text": "No outstanding receivables — every submitted invoice is paid.", "data": []}
    lines = [f"{r['customer']}: {r['amount']} outstanding ({r['status']}, inv {r['number']})" for r in ar[:10]]
    total = sum(r.get("amount_val", 0) for r in ar)
    return {"text": f"{len(ar)} open invoice(s), ${total:,.0f} owed to you:\n" + "\n".join(lines), "data": ar}


def _t_find_party(a: dict) -> dict:
    q = (a.get("name") or a.get("customer") or a.get("party") or "").lower().strip()
    if not q:
        return {"text": "Which customer or supplier? Give me a name.", "data": []}
    d = fetch_activity()
    out = []
    for r in d["ar_rows"]:
        if q in (r.get("customer") or "").lower():
            out.append(f"{r['customer']} · inv {r['number']} · {r['amount']} outstanding · {r['status']}")
    for t in d["uncategorized"]:
        if q in (t.get("desc") or "").lower():
            out.append(f"bank memo '{t['desc']}' · {t['amount']} {t['direction']} · uncategorized")
    return {"text": "\n".join(out) if out else f"No open invoices or bank memos matching '{q}'.", "data": out}


def _t_close_status(_a: dict) -> dict:
    d = fetch_activity()
    pct = d["close_pct"]
    todo = [c["item"] for c in d["checklist"] if not c["done"]]
    blocking = d.get("blocking_checks", [])
    blocker = ("blocked by: " + "; ".join(c["item"] for c in blocking) if blocking
               else "all checks pass; the Period Closing Voucher can be staged for your approval")
    return {"text": f"Month-end close is {pct}% complete — {blocker}. "
            f"Remaining: {', '.join(todo) if todo else 'none'}. "
            "The agent never posts the closing voucher without human sign-off.",
            "data": d["checklist"]}


try:
    from agentic_os.governance.classification import DataClassification as _DC
    _CLS = (_DC.CUSTOMER_CONFIDENTIAL,)
except Exception:  # noqa: BLE001
    _CLS = ()

# All console tools are read-only; ERPNext writes need an operator (see /agent/run + gate.py).
_BOOKS_CONSOLE = _Assistant(
    f"{TENANT} Books", _BOOKS_PRIMER,
    tools=[
        _Tool("financials_summary", "current books KPIs", _t_summary, keywords=("summary", "kpi", "cash", "net income", "financials")),
        _Tool("list_unpaid_invoices", "open receivables", _t_unpaid, keywords=("unpaid", "owes", "owe", "receivable", "receivables")),
        _Tool("find_party", "look up a customer by name", _t_find_party, keywords=("find customer", "look up")),
        _Tool("close_status", "month-end close progress", _t_close_status, keywords=("close status", "month end", "close progress")),
    ],
    suggestions=["How do I record a customer payment?", "Who owes us money?",
                 "What's our net income?", "How does month-end close work?"],
    subtitle="Ask how to record invoices, payments and journal entries — or check A/R, A/P and the close.",
    classifications=_CLS,
)


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "core": "erpnext", "connected": erpnext_connected()}


@app.post("/api/agent")
async def api_agent(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        body = {}
    return JSONResponse(_BOOKS_CONSOLE.respond((body or {}).get("message", ""), principal=_req_principal(request)))


@app.get("/api/activity")
def activity() -> JSONResponse:
    return JSONResponse(fetch_activity())


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    page = render(fetch_activity())
    if _BOOKS_CONSOLE is not None:
        panel = "<section class='shell' style='margin-top:var(--sp-4)'>" + _BOOKS_CONSOLE.panel_html("books") + "</section>"
        page = page.replace("<footer", panel + "<footer", 1)
    return page


@app.post("/agent/run")
async def agent_run(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:
        body = {}
    action = (body or {}).get("action", "")
    handler = ACTIONS.get(action) if isinstance(action, str) else None
    if handler is None:
        return JSONResponse(
            {"status": "error", "error": f"unknown action '{action}'", "supported": sorted(ACTIONS),
             "approval_required": APPROVAL_REQUIRED},
            status_code=400,
        )
    # Server-side only: valid operator Basic auth => the route injects the approval marker; else staged preview.
    return JSONResponse(handler(body or {}, operator_approval(request)))


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive books as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_books_operator
    app.include_router(build_books_operator().router())
    # Runtime-native: register this app's governed contract at boot (guarded with the mount above).
    from agentic_os.app_kit.boot import register_app_manifest as _register_runtime_native
    from . import manifest as _runtime_native_manifest
    _register_runtime_native(_runtime_native_manifest)
except Exception as _op_exc:  # agentic_os absent / operator build failed — surface it, do not hide it
    import logging as _logging
    _logging.getLogger("agentic_os.app").error(
        "%s: operator/capability surface NOT mounted (%s: %s) — /invoke + /capabilities are unavailable",
        __name__, type(_op_exc).__name__, _op_exc)
