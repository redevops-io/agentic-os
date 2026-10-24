"""lifecycle — the email/SMS lifecycle-marketing vertical slice (the Klaviyo function),
wrapping a self-hosted **Listmonk** core (own distribution, no SaaS).

Same pattern as the other modules: wrap the running Listmonk instance with

  * an agent layer that reads REAL Listmonk data over its REST API, and
  * an MD3 dashboard (subscribers / lists / campaigns) rendered from live data.

This is the open answer to Klaviyo's **Composer** (build a campaign from one prompt)
+ predictive/assistive marketing — minus the SaaS lock-in and the 250-profile free
cap. Agent archetypes:

  POST /agent/run
    {"action":"compose_campaign","prompt":"<plain-language brief>","list_id":N}
        -> LLM writes subject + HTML body, creates a Listmonk campaign DRAFT
           (status "draft" — a human reviews + sends; never auto-blasts a list).
    {"action":"segment","goal":"<who to target>"}
        -> LLM proposes a Listmonk SQL segment over subscribers.attribs + activity.
    {"action":"suggest_flow","trigger":"welcome|abandoned_cart|winback"}
        -> LLM outlines a multi-step lifecycle flow + drafts each message.

Config (env; seed.py writes agents/lifecycle/.env):
  LISTMONK_API_URL    REST base, default http://localhost:9000
  LISTMONK_API_USER   /LISTMONK_API_TOKEN   Listmonk API user + token (Basic auth)
  LISTMONK_FRONT_URL  Listmonk UI link for the "Open in Listmonk ↗" button
  PORT                uvicorn port, default 8211
  REDEVOPS_LLM_BASE_URL / REDEVOPS_LLM_MODEL   the agent brain (DeepSeek-V4-Flash)
  (Models: the governed in-boundary endpoint only; no external provider fallback.)

Gate classification: nothing here sends to a contact. compose_campaign creates a Listmonk DRAFT (read back and
verified as `draft`; a human reviews + starts it in Listmonk), segment / suggest_flow are advisory. No action
starts, schedules or changes the status of a campaign, so none is approval-gated; test_route_gating.py pins that
the app never issues anything but the draft POST + read-back GET to Listmonk.
"""
from __future__ import annotations

import base64
import html
import json
import os

# ── enterprise permissions plane — gate tool/data access by (app, user) ──
# No-op without the context_runtime_enterprise package or CR_PERMISSIONS=1 (open-core default).
try:
    from context_runtime_enterprise.apps import bootstrap as _cr_bootstrap
    _req_principal = _cr_bootstrap("lifecycle")
except Exception:  # noqa: BLE001
    def _req_principal(request=None):
        return None
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# The Listmonk REST client, KPIs and agentic actions live in core.py (pure — no web /
# context-runtime deps) so the Mission Runtime operator can invoke them and they can be
# tested against a fake Listmonk. Import config + core helpers from there; core loads .env.
from . import core
from .core import (  # noqa: E402
    LISTMONK_API_URL, LISTMONK_API_USER, LISTMONK_API_TOKEN, LISTMONK_FRONT_URL, TENANT, SUBTITLE,
    _headers, listmonk_connected, _get, fetch_activity,
)

PORT = int(os.environ.get("PORT", "8211"))

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


app = FastAPI(title=f"lifecycle ({TENANT} · core: Listmonk)")

from agentic_os.app_kit import route_authz as _route_authz  # noqa: E402
from .route_policy import APP as _APP, ROUTE_POLICIES as _ROUTE_POLICIES  # noqa: E402
_route_authz.install(app, _APP, _ROUTE_POLICIES)   # one enforcement layer for every write route


BASE_CSS = """
:root{--surface:#131316;--surface-container-low:#1b1b1f;--surface-container:#1f1f23;
--surface-container-highest:#353539;--on-surface:#e4e2e6;--on-surface-variant:#c7c5ca;
--on-surface-muted:#918f96;--outline-variant:#2f2f33;--primary:#4fd1c5;--on-primary-container:#a8f0e6;
--primary-container:#00504a;--success:#5bd98a;--success-container:#0f3d22;--warning:#f5b544;
--warning-container:#4a3500;--danger:#f2544f;--danger-container:#5c1512;--info:#5aa9f0;--info-container:#103a5c;
--sp-1:4px;--sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-5:24px;--radius-lg:16px;--radius-pill:999px;
--font-sans:"Roboto",system-ui,sans-serif;--font-mono:"Roboto Mono",ui-monospace,monospace;}
*{box-sizing:border-box}
.page{background:var(--surface);color:var(--on-surface);font-family:var(--font-sans);padding:var(--sp-5);margin:0}
.shell{max-width:1440px;margin-inline:auto;display:flex;flex-direction:column;gap:var(--sp-5)}
.grid{display:grid;gap:var(--sp-4);grid-template-columns:repeat(12,1fr)}
.kpi-row{display:grid;gap:var(--sp-4);grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.col-4{grid-column:span 4}.col-8{grid-column:span 8}@media(max-width:839px){[class^="col-"]{grid-column:span 12}}
.card{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-4)}
.card__head{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3)}
.card__title{font:500 16px/24px var(--font-sans);margin:0}
.tile{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-4) var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-1)}
.tile__label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--on-surface-muted)}
.tile__value{font:500 32px/40px var(--font-mono)}.tile__delta{font:500 12px/16px var(--font-sans);color:var(--on-surface-variant)}
.pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:var(--radius-pill);font:500 12px/1 var(--font-sans)}
.pill--success{background:var(--success-container);color:var(--success)}.pill--warn{background:var(--warning-container);color:var(--warning)}
.pill--danger{background:var(--danger-container);color:var(--danger)}.pill--info{background:var(--info-container);color:var(--info)}
.pill--neutral{background:var(--surface-container-highest);color:var(--on-surface-variant)}.pill__dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.table{width:100%;border-collapse:collapse;font-size:14px}
.table th{text-align:left;color:var(--on-surface-muted);font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;padding:var(--sp-3) var(--sp-4);border-bottom:1px solid var(--outline-variant)}
.table td{padding:var(--sp-3) var(--sp-4);border-bottom:1px solid var(--outline-variant)}.table tbody tr:hover{background:rgba(228,226,230,.08)}
.appbar{background:var(--surface-container-low);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5)}
.appbar__row{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap}.appbar h1{margin:0;font:400 28px/36px var(--font-sans)}
.appbar__tenant{margin-top:var(--sp-3);color:var(--on-surface-variant);font:400 14px/20px}.appbar__tenant b{color:var(--on-surface)}
.appbar__sub{margin-top:var(--sp-2);color:var(--on-surface-muted);font:400 14px/20px;max-width:820px}
.spacer{flex:1}a{color:var(--primary);text-decoration:none}
.btn{display:inline-flex;align-items:center;height:36px;padding:0 16px;border-radius:var(--radius-pill);background:var(--primary-container);color:var(--on-primary-container);font:500 14px/1 var(--font-sans);border:none}
.section-label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--primary);margin:0}
.footer{color:var(--on-surface-muted);font:400 12px/16px;text-align:center;padding-top:var(--sp-2)}
"""
FONT_LINK = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
             'family=Roboto:wght@400;500&family=Roboto+Mono:wght@400;500&display=swap">')


def _esc(v) -> str:
    return html.escape(str(v))


def _camp_pill(s: str) -> str:
    return {"finished": "pill--success", "running": "pill--info", "scheduled": "pill--info",
            "draft": "pill--warn", "cancelled": "pill--danger"}.get((s or "").lower(), "pill--neutral")


def render(data: dict) -> str:
    connected = data["connected"]
    conn = "core: Listmonk connected" if connected else "core: Listmonk UNREACHABLE"
    cls = "pill--success" if connected else "pill--danger"
    kpis = "".join(f"<div class='tile'><div class='tile__label'>{_esc(k['label'])}</div>"
                   f"<div class='tile__value'>{_esc(k['value'])}</div>"
                   f"<div class='tile__delta'>{_esc(k['note'])}</div></div>" for k in data["kpis"])
    lists = "".join(f"<tr><td>{_esc(l['name'])}</td><td>{_esc(l['type'])}</td>"
                    f"<td style='font-family:var(--font-mono)'>{_esc(l['subs'])}</td></tr>" for l in data["lists"]) \
        or "<tr><td colspan=3>No lists yet.</td></tr>"
    camps = "".join(f"<tr><td>{_esc(c['name'])}</td><td>{_esc(c['subject'])}</td>"
                    f"<td><span class='pill {_camp_pill(c['status'])}'>{_esc((c['status'] or '').upper())}</span></td>"
                    f"<td>{_esc(c['lists'])}</td><td style='font-family:var(--font-mono)'>{_esc(c['sent'])}</td></tr>"
                    for c in data["recent"]) or "<tr><td colspan=5>No campaigns yet.</td></tr>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lifecycle — {_esc(TENANT)}</title>{FONT_LINK}<style>{BASE_CSS}</style></head>
<body class="page"><div class="shell">
<header class="appbar"><div class="appbar__row"><h1>Lifecycle Marketing</h1>
<span class="pill {cls}"><span class="pill__dot"></span>agent active · {_esc(conn)}</span>
<span class="pill pill--info"><span class="pill__dot"></span>data: live from Listmonk</span>
<span class="spacer"></span><a class="btn" href="{_esc(data['front_url'])}" target="_blank" rel="noopener">Open in Listmonk ↗</a></div>
<div class="appbar__tenant"><b>{_esc(TENANT)}</b> · core: Listmonk (open-source email/SMS) · the Klaviyo function, self-hosted</div>
<div class="appbar__sub">{_esc(SUBTITLE)}</div></header>
<section class='kpi-row'>{kpis}</section>
<section class='shell' style='gap:var(--sp-4)'><div class='section-label'>Lifecycle marketing</div><div class='grid'>
<div class='col-4'><div class='card'><div class='card__head'><h2 class='card__title'>Lists (live)</h2></div>
<table class='table'><thead><tr><th>List</th><th>Type</th><th>Subs</th></tr></thead><tbody>{lists}</tbody></table></div></div>
<div class='col-8'><div class='card'><div class='card__head'><h2 class='card__title'>Recent campaigns (live)</h2>
<span class='pill pill--info'><span class='pill__dot'></span>data: live from Listmonk</span></div>
<table class='table'><thead><tr><th>Campaign</th><th>Subject</th><th>Status</th><th>Lists</th><th>Sent</th></tr></thead><tbody>{camps}</tbody></table></div></div>
</div></section>
<footer class="footer">lifecycle · live for {_esc(TENANT)} · <a href="/api/activity">/api/activity</a> ·
compose · segment · flows · redevops.io Agentic Business OS</footer></div></body></html>"""


# --- governed LLM (N7) -------------------------------------------------------
# Brief/goal text (subscriber-targeting context) goes into these prompts, so every model call routes through the
# GovernedModelRouter (agentic_os.app.llm.governed_text) as CUSTOMER_CONFIDENTIAL: only the configured IN-BOUNDARY
# endpoint can receive it; None comes back -- never a public fallback -- when there is none or the router refuses.
def _llm_text(prompt: str, max_tokens: int = 600) -> str | None:
    try:
        from agentic_os.app.llm import governed_text
        from agentic_os.governance.classification import DataClassification
    except Exception:  # noqa: BLE001
        return None
    return governed_text(prompt, classifications=(DataClassification.CUSTOMER_CONFIDENTIAL,),
                         max_tokens=max_tokens, temperature=0.5)


# --- agentic actions (thin wrappers over core; core stays context-runtime-free) ---
# The pure Listmonk work lives in core.py; app.py only injects the LLM copy callback
# (`_llm_text`, the governed in-boundary router). With the callback absent core still
# runs deterministically — compose posts a draft, segment/suggest_flow return skeletons.
def _compose_campaign(body: dict) -> dict:
    """Compose a Listmonk campaign draft over the real core, with LLM-written copy."""
    return core.compose_campaign(body, llm=_llm_text)


def _segment(body: dict) -> dict:
    """Propose a Listmonk SQL segment for a targeting goal (advisory, read-only)."""
    return core.segment(body, llm=_llm_text)


def _suggest_flow(body: dict) -> dict:
    """Outline a multi-step lifecycle flow (advisory, read-only)."""
    return core.suggest_flow(body, llm=_llm_text)


_ACTIONS = {"compose_campaign": _compose_campaign, "segment": _segment, "suggest_flow": _suggest_flow}


# ──────── conversational agent: governed LLM + read-only tools ────────

_LIFECYCLE_PRIMER = """A SUBSCRIBER is a contact who can receive email/SMS. Subscribers live in one or more LISTS. Import them under Subscribers → Import, or add one manually.

A LIST is an audience. Lists are single or double OPT-IN — double opt-in emails a confirmation link before anyone is subscribed, which keeps deliverability high. Create one under Lists → New.

A CAMPAIGN is one send to one or more lists. It moves through statuses: DRAFT → SCHEDULED → RUNNING → FINISHED. Create it under Campaigns → New, pick lists, write the subject + body from a TEMPLATE, then start or schedule it.

A TEMPLATE is the reusable HTML wrapper a campaign renders into. A SEGMENT is a saved SQL-like query over subscriber attributes so you target only who you want.

OPENS and CLICKS are tracked per campaign when tracking is on. Use them to compare subjects and lists. TRANSACTIONAL messages (receipts, resets) are sent via the API, separate from campaigns.

This app is the Klaviyo function self-hosted on Listmonk: the agent can compose a campaign draft, segment an audience, or suggest a lifecycle flow — but a human reviews and sends from Listmonk."""


def _t_lifecycle_summary(_a: dict) -> dict:
    d = fetch_activity()
    k = {x["label"]: x["value"] for x in d["kpis"]}
    return {"text": f"{k.get('Subscribers')} subscribers across {len(d['lists'])} lists · {k.get('Campaigns sent')} campaigns "
            f"sent · {k.get('Total opens')} total opens · {k.get('Drafts ready')} drafts awaiting review.", "data": d["kpis"]}


def _t_list_campaigns(_a: dict) -> dict:
    rows = fetch_activity()["recent"]
    if not rows:
        return {"text": "No campaigns yet — create one under Campaigns → New.", "data": []}
    lines = [f"{r['name']} · {r['status']} · “{r['subject']}” ({r['views']} opens)" for r in rows[:10]]
    return {"text": "\n".join(lines), "data": rows}


def _t_list_segments(_a: dict) -> dict:
    rows = fetch_activity()["lists"]
    if not rows:
        return {"text": "No lists yet — create one under Lists → New.", "data": []}
    lines = [f"{r['name']} · {r['type']} · {r['subs']:,} subscribers" for r in rows]
    return {"text": "\n".join(lines), "data": rows}


def _agent_reply(message: str) -> dict:
    low = (message or "").lower()
    if not low.strip():
        return {"intent": "help", "text": "Ask about subscribers, campaigns and lists, or how lifecycle email works.", "evidence": []}
    for words, fn, name in ((("campaign", "recent", "draft"), _t_list_campaigns, "list_campaigns"),
                            (("list", "segment", "audience"), _t_list_segments, "list_segments"),
                            (("email", "subscriber", "doing", "summary", "open"), _t_lifecycle_summary, "lifecycle_summary")):
        if any(w in low for w in words):
            return {"intent": name, "text": fn({})["text"], "evidence": []}
    txt = _llm_text(_LIFECYCLE_PRIMER + "\n\nAnswer in 2-4 plain sentences.\nQuestion: " + message, 300)
    return {"intent": "help", "text": txt or "The assistant model is offline (no in-boundary model configured).",
            "evidence": []}


_AGENT_PANEL = (
    "<section class='shell' style='margin-top:16px'><h3>Assistant</h3>"
    "<input id='agmsg' placeholder='Ask about campaigns and lists' style='width:70%'> "
    "<button onclick=\"fetch('api/agent',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({message:document.getElementById('agmsg').value})}).then(r=>r.json())"
    ".then(d=>{document.getElementById('agout').textContent=d.text})\">Ask</button>"
    "<pre id='agout' style='white-space:pre-wrap'></pre></section>")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "core": "listmonk", "connected": listmonk_connected()}


@app.post("/api/agent")
async def api_agent(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        body = {}
    return JSONResponse(_agent_reply((body or {}).get("message", "")))


@app.get("/api/activity")
def activity() -> JSONResponse:
    return JSONResponse(fetch_activity())


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    page = render(fetch_activity())
    return page.replace("<footer", _AGENT_PANEL + "<footer", 1)


@app.post("/agent/run")
async def agent_run(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:
        body = {}
    action = (body or {}).get("action", "")
    if action in _ACTIONS:
        return JSONResponse(_ACTIONS[action](body or {}))
    return JSONResponse({"status": "error", "error": f"unknown action '{action}'",
                         "supported": list(_ACTIONS)}, status_code=400)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive lifecycle as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_lifecycle_operator
    app.include_router(build_lifecycle_operator().router())
    # Runtime-native: register this app's governed contract at boot (guarded with the mount above).
    from agentic_os.app_kit.boot import register_app_manifest as _register_runtime_native
    from . import manifest as _runtime_native_manifest
    _register_runtime_native(_runtime_native_manifest)
except Exception as _op_exc:  # agentic_os absent / operator build failed — surface it, do not hide it
    import logging as _logging
    _logging.getLogger("agentic_os.app").error(
        "%s: operator/capability surface NOT mounted (%s: %s) — /invoke + /capabilities are unavailable",
        __name__, type(_op_exc).__name__, _op_exc)
