"""agentic-billing — the FIRST "agentic module on a real OSS core" vertical slice.

This is the reference implementation every other module copies. It wraps the running
self-hosted Lago instance (the OSS billing core) with:

  * an agent layer that reads REAL Lago data over the REST API, and
  * an MD3 dashboard (same design tokens as deploy/module_service.py) rendered from
    that live data — no mock data.

Pattern for the other 8 cores:
  1. point CORE_API_URL / CORE_API_KEY at the running OSS core,
  2. write a `fetch_*` that pulls real records + a `compute_kpis`,
  3. reuse BASE_CSS + the render helpers below,
  4. add agentic actions in /agent/run that are deterministic core API calls, with a
     human-approval gate on anything that moves money.

Endpoints:
  GET  /health        -> {"status","core":"lago","connected": <bool from Lago /health>}
  GET  /api/activity  -> live KPIs + recent invoices derived from Lago REST
  GET  /              -> MD3 billing dashboard rendered from the live data
  POST /agent/run     -> agentic action: {"action":"chase_overdue"|"refund"}

Config (env; seed.py writes agents/billing/.env automatically):
  LAGO_API_URL    REST base, default http://localhost:3000
  LAGO_API_KEY    Bearer token (the ApiKey value from the seed)
  LAGO_FRONT_URL  Lago UI link for the hybrid/human-operable "Open in Lago" button
  PORT            uvicorn port, default 8201
  REDEVOPS_LLM_BASE_URL  OPTIONAL in-boundary model for the narration blurb (agentic_os.app.llm.governed_text,
                     CUSTOMER_CONFIDENTIAL, fail-closed); the endpoint works fully without it.
  OPERATOR_BASIC_AUTH  ``user:pass`` -- a request carrying these Basic credentials is the operator's approval
                     for /agent/run actions; everything else only gets a staged preview.
"""
from __future__ import annotations

import html
import os

# ── enterprise permissions plane — gate tool/data access by (app, user) ──
# No-op without the context_runtime_enterprise package or CR_PERMISSIONS=1 (open-core default).
try:
    from context_runtime_enterprise.apps import bootstrap as _cr_bootstrap
    _req_principal = _cr_bootstrap("billing")
except Exception:  # noqa: BLE001
    def _req_principal(request=None):
        return None
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# --- config ------------------------------------------------------------------
# The Lago REST client, KPIs and agentic actions live in core.py (pure — no web /
# context-runtime deps) so the Mission Runtime operator can invoke them and they can be
# tested against a fake Lago. Import config + core helpers from there; core loads .env.
from . import core
from .gate import operator_approval
from .core import (  # noqa: E402
    LAGO_API_URL, LAGO_API_KEY, LAGO_FRONT_URL, TENANT, SUBTITLE,
    _headers, lago_connected, _get_all, _money, _invoice_state, _days_overdue, fetch_activity,
)

PORT = int(os.environ.get("PORT", "8201"))

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


app = FastAPI(title="agentic-billing (Meridian Wealth Management · core: Lago)")


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
.barlist__row{display:grid;grid-template-columns:160px 1fr 88px;align-items:center;gap:var(--sp-4)}
.barlist__label{color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}
.barlist__pct{text-align:right;font-family:var(--font-mono);font-feature-settings:"tnum";font-size:13px;color:var(--on-surface-variant)}
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


def _state_pill(state: str) -> str:
    s = state.upper()
    if s == "PAID":
        return "pill--success"
    if s in ("OVERDUE", "FAILED"):
        return "pill--danger"
    if s in ("DRAFT", "SENT", "PENDING"):
        return "pill--warn"
    return "pill--neutral"


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
    """Show when there's an overdue invoice (agent can act) or a pending refund."""
    overdue = data.get("overdue", [])
    if not overdue:
        return ""
    first = overdue[0]
    extra = f" (+{len(overdue) - 1} more)" if len(overdue) > 1 else ""
    return (
        "<div class='banner'>"
        f"<span class='pill pill--warn'><span class='pill__dot'></span>{len(overdue)} need attention</span>"
        "<span class='label-m' style='text-transform:uppercase;color:var(--warning)'>chase_overdue</span>"
        f"<span class='body-m'>Overdue: {_esc(first['customer'])} — {_esc(first['number'])} "
        f"{_esc(first['amount'])}, {_esc(first['days_overdue'])} days overdue{_esc(extra)}. "
        "Agent can send a dunning reminder / retry payment (hybrid, approval-gated).</span>"
        "</div>"
    )


def _collected_outstanding_bars(data: dict) -> str:
    """Stripe-style collected-vs-outstanding meter from the live KPI amounts."""
    def cents(label):
        for k in data["kpis"]:
            if k["label"] == label:
                return float(k["value"].replace("$", "").replace(",", "") or 0)
        return 0.0

    collected = cents("Collected MTD")
    outstanding = cents("Outstanding")
    total = max(collected + outstanding, 1)
    rows = [
        ("Collected (paid)", collected),
        ("Outstanding (open)", outstanding),
    ]
    body = ""
    for label, val in rows:
        pct = int(round(100 * val / total))
        body += (
            "<div class='barlist__row'>"
            f"<div class='barlist__label'>{_esc(label)}</div>"
            f"<div class='bar'><span style='width:{pct}%'></span></div>"
            f"<div class='barlist__pct'>${val:,.0f}</div>"
            "</div>"
        )
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Collected vs outstanding (live)</h2></div>"
        f"<div class='barlist'>{body}</div>"
        "</div>"
    )


def _invoice_table(data: dict) -> str:
    rows = ""
    for inv in data["recent"]:
        state = inv["state"]
        label = state
        if state == "OVERDUE" and inv.get("days_overdue") is not None:
            label = f"OVERDUE {inv['days_overdue']}d"
        rows += (
            "<tr>"
            f"<td>{_esc(inv['number'])}</td>"
            f"<td>{_esc(inv['customer'])}</td>"
            f"<td class='num'>{_esc(inv['amount'])}</td>"
            f"<td><span class='pill {_state_pill(state)}'>{_esc(label)}</span></td>"
            "</tr>"
        )
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Recent invoices</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from Lago</span></div>"
        "<table class='table'><thead><tr><th>Invoice</th><th>Customer</th><th>Amount</th><th>Status</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "</div>"
    )


def render(data: dict) -> str:
    connected = data["connected"]
    conn_txt = "core: Lago connected" if connected else "core: Lago UNREACHABLE"
    conn_cls = "pill--success" if connected else "pill--danger"
    status_pill = (
        f"<span class='pill {conn_cls}'><span class='pill__dot'></span>agent active · {_esc(conn_txt)}</span>"
    )
    live_badge = "<span class='pill pill--info'><span class='pill__dot'></span>data: live from Lago</span>"
    open_btn = f"<a class='btn' href='{_esc(data['front_url'])}' target='_blank' rel='noopener'>Open in Lago ↗</a>"

    body = (
        _approval_banner(data)
        + _kpi_tiles(data["kpis"])
        + "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Cash position</div>"
        "<div class='grid'>"
        f"<div class='col-6'>{_collected_outstanding_bars(data)}</div>"
        f"<div class='col-6'>{_invoice_table(data)}</div>"
        "</div></section>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentic Billing — {_esc(TENANT)}</title>
{FONT_LINK}
<style>{BASE_CSS}{PAGE_CSS}</style>
</head>
<body class="page">
<div class="shell">
  <header class="appbar">
    <div class="appbar__row">
      <h1>Agentic Billing</h1>
      {status_pill}
      {live_badge}
      <span class="spacer"></span>
      {open_btn}
    </div>
    <div class="appbar__tenant"><b>{_esc(TENANT)}</b> · core: Lago (open-source billing)</div>
    <div class="appbar__sub">{_esc(SUBTITLE)}</div>
  </header>
  {body}
  <footer class="footer">agentic-billing · live activity for {_esc(TENANT)} ·
    <a href="/api/activity">/api/activity</a> · agent + human, on a real Lago core · redevops.io Agentic Business OS</footer>
</div>
</body>
</html>"""


# --- optional LLM reasoning blurb (governed; works without any model) --------
def _llm_blurb(prompt: str) -> str | None:
    """A one-line narration from the GOVERNED in-boundary model, or None.

    N7: the prompt carries customer names and amounts, so it is classified CUSTOMER_CONFIDENTIAL and routed
    by ``agentic_os.app.llm.governed_text`` -- fail closed (None) when the router refuses or no in-boundary
    endpoint is configured; there is no direct-HTTP fallback. Every action is deterministic Lago API work;
    the model only narrates."""
    from .assistant import governed_answer
    try:
        from agentic_os.governance.classification import DataClassification
    except Exception:  # noqa: BLE001
        return None
    return governed_answer(prompt, (DataClassification.CUSTOMER_CONFIDENTIAL,), max_tokens=220, temperature=0.3)


def _with_approval(body: dict, approval) -> dict:
    """The core call's body: every caller-supplied ``_approval*`` key is stripped (an LLM-chosen tool argument
    is as untrusted as an HTTP body), then the route's server-side marker -- if any -- is set."""
    clean = _strip_approval(body or {})
    return {**clean, "_approval": approval} if approval else clean


# --- agentic actions (thin wrappers over core; the approval gate lives IN core) ---
# ``approval`` is None for every caller except a route that proved operator Basic auth (see gate.py): the
# console/LLM tools below never pass one, so they can only ever stage a preview.
def _chase_overdue(approval=None) -> dict:
    """Dunning over the real Lago core (staged unless approved), with the optional governed narration."""
    return core.chase_overdue(blurb=_llm_blurb, approval=approval)


def _refund(body: dict, approval=None) -> dict:
    """Refund: staged unless approved (never auto-executed)."""
    return core.stage_refund(_with_approval(body, approval))


def _create_subscription(body: dict, approval=None) -> dict:
    """Onboard a customer onto a paid plan on the real Lago core (staged unless approved)."""
    return core.create_subscription(_with_approval(body, approval))


def _cancel_subscription(body: dict, approval=None) -> dict:
    """Compensating action (saga undo) for _create_subscription (staged unless approved)."""
    return core.cancel_subscription(_with_approval(body, approval))


#: every /agent/run action -> handler(body, approval). The route is the ONLY place the operator marker enters.
ACTIONS = {
    "chase_overdue": lambda body, approval: _chase_overdue(approval),
    "refund": _refund,
    "create_subscription": _create_subscription,
    "cancel_subscription": _cancel_subscription,
}


# ──────── conversational agent · Context Runtime (LLM) + redevops-rag (grounding) + agent-harness (tools) ────────
from .assistant import Assistant as _Assistant, Tool as _Tool

_BILLING_PRIMER = """A CUSTOMER is who you bill. Create one under Customers → Add customer (name, email, billing address).

A PLAN is what a customer pays for and how often. Create it under Plans → Add plan: set the interval (weekly / monthly / yearly) and add CHARGES — a flat recurring fee and/or metered charges billed on usage.

A SUBSCRIPTION assigns a plan to a customer. Open the customer → Add subscription → pick the plan. Lago then issues an INVOICE automatically at the end of each billing period.

An INVOICE has a payment status. OUTSTANDING means finalized but not yet paid. OVERDUE means past its due date. DUNNING is the automatic chase: Lago retries payment and emails the customer. Here the agent's "chase overdue" runs that retry across every overdue invoice at once.

A REFUND is issued as a CREDIT NOTE on an invoice (invoice → Issue credit note). Refunds move money out, so the agent only STAGES them for human approval — it never auto-executes a refund.

Payment success rate is paid ÷ finalized invoices. A WALLET holds prepaid credits; a COUPON discounts an invoice; an ADD-ON is a one-off charge outside the plan."""


def _t_summary(_a: dict) -> dict:
    d = fetch_activity()
    k = {x["label"]: x["value"] for x in d["kpis"]}
    c = d["counts"]
    return {"text": f"Collected MTD {k.get('Collected MTD')}, outstanding {k.get('Outstanding')}, payment success "
            f"{k.get('Payment success')}. {c['customers']} customers · {c['invoices']} invoices · {c['overdue']} overdue.",
            "data": d["kpis"]}


def _t_overdue(_a: dict) -> dict:
    od = fetch_activity()["overdue"]
    if not od:
        return {"text": "No overdue invoices right now — collections are clean.", "data": []}
    lines = [f"{r['customer']}: {r['amount']} · {r['days_overdue']}d overdue (inv {r['number']})" for r in od[:10]]
    return {"text": f"{len(od)} overdue invoice(s):\n" + "\n".join(lines), "data": od}


def _t_find_customer(a: dict) -> dict:
    import re as _re
    q = (a.get("name") or a.get("customer") or "").lower().strip()
    if not q and a.get("message"):        # console free text: "find customer Acme" / "invoices for Acme"
        q = _re.sub(r"\b(find|customer|invoices?|for|of|show|me|the)\b", " ", a["message"].lower()).strip(" ?.!")
    if not q:
        return {"text": "Which customer? Give me a name.", "data": []}
    d = fetch_activity()
    seen: set = set()
    out = []
    for r in d["recent"] + d["overdue"]:
        if q in (r.get("customer") or "").lower() and r.get("number") not in seen:
            seen.add(r.get("number"))
            out.append(f"{r['customer']} · inv {r['number']} · {r['amount']} · {r.get('state', r.get('days_overdue', ''))}")
    return {"text": "\n".join(out) if out else f"No invoices found for '{q}'.", "data": out}


def _t_chase(_a: dict) -> dict:
    r = _chase_overdue()
    return {"text": r.get("summary") or f"Chased overdue invoices · {r.get('status', 'done')}.", "data": r}


def _t_refund(a: dict) -> dict:
    r = _refund(a or {})
    return {"text": r.get("summary", "Refund staged for human approval."), "data": r}


try:
    from agentic_os.governance.classification import DataClassification as _DC
    _CLS = (_DC.CUSTOMER_CONFIDENTIAL,)
except Exception:  # noqa: BLE001
    _CLS = ()

# Console tools NEVER receive an approval marker: chase_overdue/refund only stage a preview from here.
_BILL_CONSOLE = _Assistant(
    f"{TENANT} Billing", _BILLING_PRIMER,
    tools=[
        _Tool("billing_summary", "current billing KPIs", _t_summary, keywords=("kpi", "summary", "collected", "outstanding", "billing")),
        _Tool("list_overdue", "list overdue invoices", _t_overdue, keywords=("overdue", "owes", "owe", "late")),
        _Tool("find_customer", "find a customer's invoices by name", _t_find_customer, keywords=("find customer", "invoices for")),
        _Tool("chase_overdue", "stage dunning on all overdue invoices (needs operator approval)", _t_chase,
              keywords=("chase", "dunning run", "collect"), side_effecting=True),
        _Tool("refund", "stage a refund/credit note for human approval", _t_refund, keywords=("refund", "credit note"),
              side_effecting=True),
    ],
    suggestions=["Who's overdue?", "How do I create a plan?", "Chase overdue invoices", "What is dunning?"],
    subtitle="Ask about invoices, plans and dunning. Money-moving actions are staged for operator approval.",
    classifications=_CLS,
)


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "core": "lago", "connected": lago_connected()}


@app.post("/api/agent")
async def api_agent(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        body = {}
    return JSONResponse(_BILL_CONSOLE.respond((body or {}).get("message", ""), principal=_req_principal(request)))


@app.get("/api/activity")
def activity() -> JSONResponse:
    return JSONResponse(fetch_activity())


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    page = render(fetch_activity())
    if _BILL_CONSOLE is not None:
        panel = "<section class='shell' style='margin-top:var(--sp-4)'>" + _BILL_CONSOLE.panel_html("bill") + "</section>"
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
            {"status": "error", "error": f"unknown action '{action}'", "supported": sorted(ACTIONS)},
            status_code=400,
        )
    # Server-side only: valid operator Basic auth => the route injects the approval marker; else staged preview.
    return JSONResponse(handler(body or {}, operator_approval(request)))


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive billing as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_billing_operator
    app.include_router(build_billing_operator().router())
    from .loop_api import router as _loop_router   # closed loop: POST /api/decide (+ /{mission_id}/approve)
    app.include_router(_loop_router)
    # Runtime-native: register this app's governed contract at boot (guarded with the mount above).
    from agentic_os.app_kit.boot import register_app_manifest as _register_runtime_native
    from . import manifest as _runtime_native_manifest
    _register_runtime_native(_runtime_native_manifest)
except Exception as _op_exc:  # agentic_os absent / operator build failed — surface it, do not hide it
    import logging as _logging
    _logging.getLogger("agentic_os.app").error(
        "%s: operator/capability surface NOT mounted (%s: %s) — /invoke + /capabilities are unavailable",
        __name__, type(_op_exc).__name__, _op_exc)
