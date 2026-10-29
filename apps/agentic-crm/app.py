"""agentic-crm — the sales/CRM vertical slice, wrapping the live ERPNext CRM core.

Same pattern as agentic-support / agentic-books: wrap the running self-hosted
**ERPNext** instance (its CRM doctypes — Lead, Opportunity, Customer, Contact)
with

  * an agent layer that reads REAL ERPNext CRM data over the REST API, and
  * an MD3 dashboard (pipeline / lead-queue layout, same design tokens as the
    other modules) rendered from that live data — no mock data.

This is the open, self-hosted answer to Salesforce Agentforce's Sales Development
Agent and HubSpot Breeze's Prospecting Agent: the CRM record system is already
running (ERPNext); we add the agent archetypes on top —

  POST /agent/run
    {"action":"score_lead","lead":"<name>"}     -> LLM scores the lead 0-100 +
        rationale, written back as a Comment on the Lead (human-auditable).
    {"action":"research","lead":"<name>"}        -> LLM synthesises a firmographic
        + buying-signal brief from the lead's known fields (wire DEERFLOW_URL to
        pull real web signals; falls back to reasoning over known data).
    {"action":"draft_outreach","lead":"<name>"}  -> personalised first-touch email,
        saved as a Comment (a human sends it — never auto-emails the prospect).
    {"action":"qualify","lead":"<name>"}         -> advance Lead status (e.g. Open
        -> Replied/Opportunity), the pipeline-progression action.
    {"action":"ask","q":"<natural language>"}    -> NL-to-CRM: the LLM answers a
        question over the live pipeline (the Twenty/Headless-360 pattern).

Config (env; seed.py writes agents/agentic-crm/.env):
  ERPNEXT_URL        REST base, default http://localhost:8092
  ERPNEXT_API_KEY    /ERPNEXT_API_SECRET   token auth (same as agentic-books)
  ERPNEXT_FRONT_URL  ERPNext UI link for the "Open in ERPNext ↗" button
  PORT               uvicorn port, default 8210
  REDEVOPS_LLM_BASE_URL / REDEVOPS_LLM_MODEL   the agent brain (model from REDEVOPS_LLM_MODEL)
  DEERFLOW_URL       OPTIONAL — web-research gateway for real enrichment signals
  OPERATOR_BASIC_AUTH  `user:pass` operator credential; `qualify` (a Lead status change) executes only with it,
                     otherwise /agent/run returns a staged pending_approval preview.
  (Models: governed in-boundary endpoint only; no external provider fallback.)
"""
from __future__ import annotations

import html
import os

# ── enterprise permissions plane — gate tool/data access by (app, user) ──
# Basic default without the enterprise package and without CR_PERMISSIONS / CR_IDENTITY_*; if either is set and the gate cannot
# be installed, agentic_os.app_gate refuses to start the app (it never runs ungated by accident).
from agentic_os.app_gate import RequestContext as _RequestContext, denial_result as _denial_result, deny_response as _deny_response, install_gate as _install_gate
from agentic_os.app_gate import with_authorization as _with_authorization
_GATE_APP = "agentic-crm"
_req_principal = _install_gate(_GATE_APP)   # starts gated or not at all when permissions were requested (RR-01)

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# --- config ------------------------------------------------------------------
# The ERPNext REST client, pipeline KPIs and agentic actions live in core.py (pure — no web /
# context-runtime deps) so the Mission Runtime operator can invoke them and they can be
# tested against a fake ERPNext. Import config + core helpers from there; core loads .env.
from . import core
from .core import (  # noqa: E402
    ERPNEXT_URL, ERPNEXT_API_KEY, ERPNEXT_API_SECRET, ERPNEXT_FRONT_URL, TENANT, SUBTITLE,
    _headers, erp_connected, _list, _get_doc, _add_comment, _set_field, _f, fetch_activity,
    _lead_brief, _research_signals,
)

PORT = int(os.environ.get("PORT", "8210"))

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


app = FastAPI(title=f"agentic-crm ({TENANT} · core: ERPNext CRM)")

from agentic_os.app_kit import route_authz as _route_authz  # noqa: E402
from .route_policy import APP as _APP, ROUTE_POLICIES as _ROUTE_POLICIES  # noqa: E402
_route_authz.install(app, _APP, _ROUTE_POLICIES)   # one enforcement layer for every write route
app.add_middleware(_RequestContext)   # the active gate (basic or enterprise) sees THIS request's principal in every route


# --- MD3 styling (house design tokens, shared across modules) -----------------
BASE_CSS = """
:root{--surface:#131316;--surface-container-low:#1b1b1f;--surface-container:#1f1f23;
--surface-container-high:#2a2a2e;--surface-container-highest:#353539;--on-surface:#e4e2e6;
--on-surface-variant:#c7c5ca;--on-surface-muted:#918f96;--outline-variant:#2f2f33;
--primary:#4fd1c5;--on-primary-container:#a8f0e6;--primary-container:#00504a;
--success:#5bd98a;--success-container:#0f3d22;--warning:#f5b544;--warning-container:#4a3500;
--danger:#f2544f;--danger-container:#5c1512;--info:#5aa9f0;--info-container:#103a5c;
--sp-1:4px;--sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-5:24px;--radius-md:12px;--radius-lg:16px;--radius-pill:999px;
--font-sans:"Roboto",system-ui,sans-serif;--font-mono:"Roboto Mono",ui-monospace,monospace;}
*{box-sizing:border-box}
.page{background:var(--surface);color:var(--on-surface);font-family:var(--font-sans);padding:var(--sp-5);margin:0}
.shell{max-width:1440px;margin-inline:auto;display:flex;flex-direction:column;gap:var(--sp-5)}
.grid{display:grid;gap:var(--sp-4);grid-template-columns:repeat(12,1fr)}
.kpi-row{display:grid;gap:var(--sp-4);grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.col-4{grid-column:span 4}.col-8{grid-column:span 8}.col-12{grid-column:span 12}
@media(max-width:839px){[class^="col-"]{grid-column:span 12}}
.card{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-4)}
.card__head{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3)}
.card__title{font:500 16px/24px var(--font-sans);color:var(--on-surface);margin:0}
.tile{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-4) var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-1)}
.tile__label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--on-surface-muted)}
.tile__value{font:500 32px/40px var(--font-mono);color:var(--on-surface)}
.tile__delta{font:500 12px/16px var(--font-sans);color:var(--on-surface-variant)}
.pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:var(--radius-pill);font:500 12px/1 var(--font-sans)}
.pill--success{background:var(--success-container);color:var(--success)}.pill--warn{background:var(--warning-container);color:var(--warning)}
.pill--danger{background:var(--danger-container);color:var(--danger)}.pill--info{background:var(--info-container);color:var(--info)}
.pill--neutral{background:var(--surface-container-highest);color:var(--on-surface-variant)}
.pill__dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.table{width:100%;border-collapse:collapse;font-size:14px}
.table th{text-align:left;color:var(--on-surface-muted);font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;padding:var(--sp-3) var(--sp-4);border-bottom:1px solid var(--outline-variant)}
.table td{padding:var(--sp-3) var(--sp-4);color:var(--on-surface);border-bottom:1px solid var(--outline-variant)}
.table tbody tr:hover{background:rgba(228,226,230,.08)}
.bar{height:8px;border-radius:var(--radius-pill);background:var(--surface-container-highest);overflow:hidden}.bar>span{display:block;height:100%;background:var(--primary)}
.appbar{background:var(--surface-container-low);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5)}
.appbar__row{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap}.appbar h1{margin:0;font:400 28px/36px var(--font-sans)}
.appbar__tenant{margin-top:var(--sp-3);color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}.appbar__tenant b{color:var(--on-surface)}
.appbar__sub{margin-top:var(--sp-2);color:var(--on-surface-muted);font:400 14px/20px var(--font-sans);max-width:820px}
.spacer{flex:1}a{color:var(--primary);text-decoration:none}
.btn{display:inline-flex;align-items:center;gap:6px;height:36px;padding:0 16px;border-radius:var(--radius-pill);background:var(--primary-container);color:var(--on-primary-container);font:500 14px/1 var(--font-sans);border:none}
.section-label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--primary);margin:0}
.barlist{display:flex;flex-direction:column;gap:var(--sp-4)}.barlist__row{display:grid;grid-template-columns:160px 1fr 96px;align-items:center;gap:var(--sp-4)}
.barlist__label{color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}.barlist__pct{text-align:right;font-family:var(--font-mono);font-size:13px;color:var(--on-surface-variant)}
.footer{color:var(--on-surface-muted);font:400 12px/16px var(--font-sans);text-align:center;padding-top:var(--sp-2)}
"""
FONT_LINK = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
             'family=Roboto:wght@400;500&family=Roboto+Mono:wght@400;500&display=swap">')


def _esc(v) -> str:
    return html.escape(str(v))


def _status_pill(status: str) -> str:
    s = (status or "").lower()
    if s in ("converted", "customer", "won"):
        return "pill--success"
    if s in ("opportunity", "replied", "quotation"):
        return "pill--info"
    if s in ("lost", "do not contact"):
        return "pill--danger"
    return "pill--neutral"


def _kpi_tiles(kpis: list[dict]) -> str:
    cells = "".join(
        "<div class='tile'>"
        f"<div class='tile__label'>{_esc(k['label'])}</div>"
        f"<div class='tile__value'>{_esc(k['value'])}</div>"
        f"<div class='tile__delta'>{_esc(k['note'])}</div></div>"
        for k in kpis)
    return f"<section class='kpi-row'>{cells}</section>"


def _stage_bars(data: dict) -> str:
    rows = "".join(
        "<div class='barlist__row'>"
        f"<div class='barlist__label'>{_esc(s['label'])}</div>"
        f"<div class='bar'><span style='width:{int(s['pct'])}%'></span></div>"
        f"<div class='barlist__pct'>${int(s['value']):,}</div></div>"
        for s in data.get("stages", []))
    return ("<div class='card'><div class='card__head'>"
            "<h2 class='card__title'>Pipeline by stage (live)</h2>"
            "<span class='pill pill--info'><span class='pill__dot'></span>data: live from ERPNext</span></div>"
            f"<div class='barlist'>{rows or '<div class=barlist__label>No open opportunities yet.</div>'}</div></div>")


def _queue_table(data: dict) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{_esc(t['lead'])}</td>"
        f"<td>{_esc(t['company'])}</td>"
        f"<td><span class='pill {_status_pill(t['status'])}'>{_esc((t['status'] or '').upper())}</span></td>"
        f"<td>{_esc(t['source'])}</td>"
        f"<td>{_esc(t['email'])}</td></tr>"
        for t in data.get("queue", []))
    return ("<div class='card'><div class='card__head'>"
            "<h2 class='card__title'>Lead queue — agent work list</h2>"
            "<span class='pill pill--info'><span class='pill__dot'></span>data: live from ERPNext</span></div>"
            "<table class='table'><thead><tr><th>Lead</th><th>Company</th><th>Status</th><th>Source</th><th>Email</th></tr></thead>"
            f"<tbody>{rows or '<tr><td colspan=5>No open leads.</td></tr>'}</tbody></table></div>")


def render(data: dict) -> str:
    connected = data["connected"]
    conn_txt = "core: ERPNext CRM connected" if connected else "core: ERPNext UNREACHABLE"
    conn_cls = "pill--success" if connected else "pill--danger"
    body = (_kpi_tiles(data["kpis"])
            + "<section class='shell' style='gap:var(--sp-4)'>"
            "<div class='section-label'>Sales pipeline</div><div class='grid'>"
            f"<div class='col-4'>{_stage_bars(data)}</div>"
            f"<div class='col-8'>{_queue_table(data)}</div></div></section>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentic CRM — {_esc(TENANT)}</title>{FONT_LINK}<style>{BASE_CSS}</style></head>
<body class="page"><div class="shell">
<header class="appbar"><div class="appbar__row"><h1>Agentic CRM</h1>
<span class="pill {conn_cls}"><span class="pill__dot"></span>agent active · {_esc(conn_txt)}</span>
<span class="pill pill--info"><span class="pill__dot"></span>data: live from ERPNext</span>
<span class="spacer"></span>
<a class="btn" href="{_esc(data['front_url'])}/app/crm" target="_blank" rel="noopener">Open in ERPNext ↗</a></div>
<div class="appbar__tenant"><b>{_esc(TENANT)}</b> · core: ERPNext CRM (open-source)</div>
<div class="appbar__sub">{_esc(SUBTITLE)}</div></header>{body}
<footer class="footer">agentic-crm · live pipeline for {_esc(TENANT)} ·
<a href="/api/activity">/api/activity</a> · score · research · draft · qualify · ask · redevops.io Agentic Business OS</footer>
</div></body></html>"""


# --- governed LLM (N7) -------------------------------------------------------
# Lead/pipeline data goes into these prompts, so every model call routes through the GovernedModelRouter
# (agentic_os.app.llm.governed_text) as CUSTOMER_CONFIDENTIAL: only the configured IN-BOUNDARY endpoint
# (REDEVOPS_LLM_BASE_URL) can receive it, and None comes back -- never a public fallback -- when there is none or
# the router refuses. core then degrades: rule-based score, `llm_unavailable` for research/draft/ask.
def _llm_text(prompt: str, max_tokens: int = 400) -> str | None:
    try:
        from agentic_os.app.llm import governed_text
        from agentic_os.governance.classification import DataClassification
    except Exception:  # noqa: BLE001
        return None
    return governed_text(prompt, classifications=(DataClassification.CUSTOMER_CONFIDENTIAL,),
                         max_tokens=max_tokens, temperature=0.3)


# --- agentic actions (thin wrappers over core; core stays context-runtime-free) ---
# The approval gate for the one status-changing action lives INSIDE core.qualify.
def _score_lead(body: dict) -> dict:
    """Score a lead over the real ERPNext core: governed LLM when it answers validly, else deterministic rules."""
    return core.score_lead(body, llm=_llm_text, model=os.environ.get("REDEVOPS_LLM_MODEL", ""))


def _research(body: dict) -> dict:
    """Enrichment brief over the real ERPNext core (optionally DEERFLOW web-enriched)."""
    return core.research(body, llm=_llm_text)


def _draft_outreach(body: dict) -> dict:
    """Draft a first-touch email (saved for human review — never auto-sent)."""
    return core.draft_outreach(body, llm=_llm_text)


def _qualify(body: dict) -> dict:
    """Advance a Lead's status (pipeline progression) — deterministic, no LLM; staged unless approved."""
    return core.qualify(body)


def _ask(body: dict) -> dict:
    """NL-to-CRM over the live pipeline snapshot, answered by _llm_text."""
    return core.ask(body, llm=_llm_text)


_ACTIONS = {"score_lead": _score_lead, "research": _research, "draft_outreach": _draft_outreach,
            "qualify": _qualify, "ask": _ask}


def _supported_actions() -> list:
    return list(_ACTIONS)


def _run_action(action: str, body: dict, authorization: "str | None") -> "dict | None":
    """THE single route -> core entry point. The operator marker is injected server-side only when the request
    carries the valid operator Basic credential; any other caller gets the core's staged preview."""
    fn = _ACTIONS.get(action)
    if fn is None:
        return None
    _tool = {"score_lead": "crm.score_lead", "research": "crm.research", "draft_outreach": "crm.draft_outreach", "qualify": "crm.qualify",
             "ask": "pipeline_summary"}[action]
    denied = _denial_result(_GATE_APP, _tool)          # authorise where it executes; the core still stages consequential actions for approval
    if denied is not None:
        return denied
    return fn(core.with_operator_approval(body, authorization))


# --- conversational agent: governed LLM + read-only tools (no operator marker, no side-effecting tool) ---
_CRM_PRIMER = """ERPNext CRM organizes your go-to-market work around core DocTypes. Leads capture marketing-sourced prospects, Contacts model the people you speak with, Customers store approved accounts, and Opportunities track revenue-bearing deals. Leads convert into Customers and Contacts when they're sales-ready, keeping unqualified data separate until the handoff is earned.

Capture inbound leads from the CRM → Lead list: click New, populate Lead Name, Company Name, Source, and Email, and set a sensible Lead Owner. Use the "Get Contacts" helper when parsing inbound email threads, and map website forms to Lead DocType fields so submissions post straight into ERPNext without manual imports.

Assign leads by using the "Assign To" sidebar or automation rules. ERPNext creates ToDo tasks and notifications automatically, and the assignee sees the lead on their Desk. Drive follow-up disciplines with the Next Contact Date/By fields so aging leads get surfaced before they stall.

Qualify by updating the Lead Status and Lead Score fields as discovery progresses. Customize status values to reflect your funnel, and rely on the Lead Owner and Campaign fields to segment performance. When a buyer is ready, use Create → Customer (and Contact) so the entire communication history flows into the account record.

When there's a defined sales cycle, create an Opportunity from that lead or customer. Fill in Sales Stage, Expected Close, and Opportunity Amount; the Pipeline Kanban view reads these fields to show weighted revenue and stage distribution. Tie opportunities to Items and Quotations when you need bill of materials detail before handoff to fulfillment.

Log every touchpoint so the timeline stays auditable: add Comments for call notes, attach replies via the Email Inbox, and record Activities for meetings. Use the CRM → Analytics reports and custom dashboards to slice pipeline by industry, territory, or owner, and export snapshots when finance needs a forecast backup."""


def _t_pipeline_summary(_args: dict) -> dict:
    data = fetch_activity()
    kpis = {item.get("label"): item.get("value") for item in data.get("kpis", []) if isinstance(item, dict)}
    counts = data.get("counts", {})
    text = (
        f"{kpis.get('Open leads', '0')} open lead(s) feeding a pipeline worth {kpis.get('Open pipeline', '$0')} "
        f"with a {kpis.get('Win rate', '0%')} win rate. "
        f"{counts.get('leads', 0)} leads · {counts.get('opps', 0)} opportunities · {counts.get('customers', 0)} customers."
    )
    return {"text": text, "data": {"kpis": data.get("kpis", []), "counts": counts}}


def _t_stage_breakdown(_args: dict) -> dict:
    data = fetch_activity()
    stages = data.get("stages", [])
    if not stages:
        return {"text": "No open opportunities in the pipeline right now.", "data": []}
    lines = []
    for stage in stages:
        label = stage.get("label", "Stage")
        pct = stage.get("pct", 0)
        value = stage.get("value", 0)
        if isinstance(value, (int, float)):
            value_text = f"${float(value):,.0f}"
        else:
            value_text = str(value)
        lines.append(f"{label}: {pct}% · {value_text}")
    return {"text": "Stage coverage:\n" + "\n".join(lines), "data": stages}


def _t_lead_queue(args: dict) -> dict:
    data = fetch_activity()
    queue = data.get("queue", [])
    if not queue:
        return {"text": "Lead queue is empty — nothing waiting on SDR follow-up.", "data": []}
    limit = args.get("limit") if isinstance(args, dict) else None
    try:
        limit_int = int(limit) if limit is not None else 5
    except (TypeError, ValueError):
        limit_int = 5
    limit_int = max(1, min(limit_int, 10))
    subset = queue[:limit_int]
    lines = []
    for entry in subset:
        lead_name = entry.get("lead") or entry.get("name") or "—"
        company = entry.get("company") or "—"
        status = entry.get("status") or "Open"
        email = entry.get("email") or "—"
        lines.append(f"{lead_name} ({company}) · status {status} · email {email}")
    return {"text": "\n".join(lines), "data": subset}


def _t_connection_status(_args: dict) -> dict:
    data = fetch_activity()
    connected = bool(data.get("connected"))
    front_url = data.get("front_url") or ERPNEXT_FRONT_URL
    if connected:
        text = (
            f"ERPNext CRM is reachable at {ERPNEXT_URL}. Use the UI link ({front_url}) to drill into records "
            "when you need full context."
        )
    else:
        text = "ERPNext CRM is currently offline — check the core service or credentials before trusting the data."
    return {"text": text, "data": {"connected": connected, "front_url": front_url}}


def _agent_reply(message: str) -> dict:
    low = (message or "").lower()
    if not low.strip():
        return {"intent": "help", "text": "Ask about the pipeline KPIs, stages, the lead queue or ERPNext status.", "evidence": []}
    for words, fn, name in ((("kpi", "pipeline", "win rate", "summary"), _t_pipeline_summary, "pipeline_summary"),
                            (("stage", "coverage"), _t_stage_breakdown, "stage_breakdown"),
                            (("queue", "lead", "next"), _t_lead_queue, "lead_queue"),
                            (("online", "connect", "erpnext", "status"), _t_connection_status, "connection_status")):
        if any(w in low for w in words):
            if (_d := _denial_result(_GATE_APP, name)):
                return {"intent": name, "text": _d["error"], "evidence": [], "denied": True}
            return {"intent": name, "text": fn({})["text"], "evidence": []}
    txt = _llm_text(_CRM_PRIMER + "\n\nAnswer in 2-4 plain sentences.\nQuestion: " + message, 300)
    return {"intent": "help", "text": txt or "The assistant model is offline (no in-boundary model configured).",
            "evidence": []}


_AGENT_PANEL = (
    "<section class='shell' style='margin-top:var(--sp-4)'><h3>Assistant</h3>"
    "<input id='agmsg' placeholder='Ask about the pipeline' style='width:70%'> "
    "<button onclick=\"fetch('api/agent',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({message:document.getElementById('agmsg').value})}).then(r=>r.json())"
    ".then(d=>{document.getElementById('agout').textContent=d.text})\">Ask</button>"
    "<pre id='agout' style='white-space:pre-wrap'></pre></section>")


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return _with_authorization(_GATE_APP, {"status": "ok", "core": "erpnext-crm", "connected": erp_connected()})


@app.post("/api/agent")
async def api_agent(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        body = {}
    return JSONResponse(_agent_reply((body or {}).get("message", "")))


@app.get("/api/activity")
def activity() -> JSONResponse:
    if (_r := _deny_response(_GATE_APP, "pipeline_summary")):
        return _r
    return JSONResponse(fetch_activity())


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    if (_r := _deny_response(_GATE_APP, "pipeline_summary")):
        return _r
    page = render(fetch_activity())
    return page.replace("<footer", _AGENT_PANEL + "<footer", 1)


@app.post("/agent/run")
async def agent_run(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    action = body.get("action", "")
    res = _run_action(action, body, request.headers.get("authorization"))
    if isinstance(res, dict) and res.get("status") == "denied" and res.get("executed") is False:      # a basic-permissions denial: 403, nothing ran
        return JSONResponse(res, status_code=403)
    if res is not None:
        return JSONResponse(res)
    return JSONResponse({"status": "error", "error": f"unknown action '{action}'",
                         "supported": _supported_actions()}, status_code=400)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    from agentic_os.app_gate import require_explicit_setting_when_exposed as _exposed
    _exposed("0.0.0.0")   # a reachable bind needs an explicit permissions setting
    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive CRM as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_crm_operator
    app.include_router(build_crm_operator().router())
    # Runtime-native: register this app's governed contract at boot (guarded with the mount above).
    from agentic_os.app_kit.boot import register_app_manifest as _register_runtime_native
    from . import manifest as _runtime_native_manifest
    _register_runtime_native(_runtime_native_manifest)
except Exception as _op_exc:  # agentic_os absent / operator build failed — surface it, do not hide it
    import logging as _logging
    _logging.getLogger("agentic_os.app").error(
        "%s: operator/capability surface NOT mounted (%s: %s) — /invoke + /capabilities are unavailable",
        __name__, type(_op_exc).__name__, _op_exc)
