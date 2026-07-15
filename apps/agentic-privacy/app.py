"""agentic-privacy — the DSAR (data-subject / consumer request) automation module.

The on-brand answer to "how do we fulfil GDPR access/erasure + CCPA/CPRA delete &
opt-out requests across all our systems?" — an agent that, on a verified request,
**fans out across every personal-data store** and assembles an access export, executes
a cascading erasure, or records an opt-out — with a tamper-evident audit log and an
SLA dashboard (GDPR 30d / CCPA 45d).

Same module pattern as agentic-crm / lifecycle: FastAPI agent + MD3 dashboard over the
live cores, brain = DeepSeek-V4-Flash (drafts the human-readable export + response).

The DSAR engine — connectors (erpnext · listmonk · chatwoot · lago · umami · zitadel · s3),
the fan-out, the audit hash-chain, and the agent actions (intake · access · delete ·
retention) — lives in core.py (pure: stdlib + httpx, no web / context-runtime deps) so the
Mission Runtime operator can invoke it and it can be tested against fake cores. This module
imports config + actions from there and keeps the FastAPI surface + the optional LLM narration.

Endpoints:
  POST /request   {"type":"access|delete|correct|opt_out","email":"...",
                   "verified":<bool>, "confirm":<bool>}
                  -> orchestrates the fan-out. DELETE requires verified + confirm
                     (otherwise a dry-run preview of what WOULD be erased). Every call
                     is written to the audit log + the requests store (SLA tracking).
  GET  /          MD3 dashboard: open/closed requests, SLA timers, per-system coverage
  GET  /api/activity   the same as JSON   ·   GET /health

NOTE: identity verification is represented by the `verified` flag — production must add
a real verification step (emailed confirmation link) before any erasure executes. This
module never deletes without verified=true AND confirm=true.
"""
from __future__ import annotations

import html
import os

# ── enterprise permissions plane — gate tool/data access by (app, user) ──
# No-op without the context_runtime_enterprise package or CR_PERMISSIONS=1 (open-core default).
try:
    from context_runtime_enterprise.apps import bootstrap as _cr_bootstrap
    _req_principal = _cr_bootstrap("agentic-privacy")
except Exception:  # noqa: BLE001
    def _req_principal(request=None):
        return None

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# --- config + the pure DSAR engine (core.py — no web / context-runtime deps) --------
from . import core
from .core import (  # noqa: E402
    TENANT, SLA_DAYS, CONNECTORS, activity, handle_request, retention_scan,
    verify_audit_chain, _fan_out, _load_requests, _save_requests, _audit, _persist,
)

PORT = int(os.environ.get("PORT", "8212"))

app = FastAPI(title=f"agentic-privacy ({TENANT})")
SUBTITLE = ("Data-subject / consumer requests (GDPR access & erasure, CCPA/CPRA delete & "
            "opt-out) fulfilled across every system — verified, audited, on the clock.")


# --- the brain (drafts the export / subject response) ------------------------
def _llm_text(prompt: str, max_tokens: int = 500) -> str | None:
    """Optional LLM narration blurb — None if no endpoint configured / any error.

    Optional by design — every core action is deterministic fan-out work; the LLM only
    narrates the access export. Absence of REDEVOPS_LLM_BASE_URL never breaks a request.
    """
    base = os.environ.get("REDEVOPS_LLM_BASE_URL")
    if not base:
        return None
    try:
        r = httpx.post(base.rstrip("/") + "/chat/completions",
                       json={"model": os.environ.get("REDEVOPS_LLM_MODEL", "DeepSeek-V4-Flash"),
                             "messages": [{"role": "user", "content": prompt}],
                             "max_tokens": max_tokens, "temperature": 0.2}, timeout=120.0)
        if r.status_code == 200:
            return (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip() or None
    except Exception:
        pass
    return None


# --- dashboard ---------------------------------------------------------------
BASE_CSS = """
:root{--surface:#131316;--surface-container-low:#1b1b1f;--surface-container:#1f1f23;
--surface-container-highest:#353539;--on-surface:#e4e2e6;--on-surface-variant:#c7c5ca;
--on-surface-muted:#918f96;--outline-variant:#2f2f33;--primary:#4fd1c5;--on-primary-container:#a8f0e6;
--primary-container:#00504a;--success:#5bd98a;--success-container:#0f3d22;--warning:#f5b544;
--warning-container:#4a3500;--danger:#f2544f;--danger-container:#5c1512;--info:#5aa9f0;--info-container:#103a5c;
--sp-1:4px;--sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-5:24px;--radius-lg:16px;--radius-pill:999px;
--font-sans:"Roboto",system-ui,sans-serif;--font-mono:"Roboto Mono",ui-monospace,monospace;}
*{box-sizing:border-box}.page{background:var(--surface);color:var(--on-surface);font-family:var(--font-sans);padding:var(--sp-5);margin:0}
.shell{max-width:1280px;margin-inline:auto;display:flex;flex-direction:column;gap:var(--sp-5)}
.kpi-row{display:grid;gap:var(--sp-4);grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.card{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-4)}
.card__head{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3)}.card__title{font:500 16px/24px var(--font-sans);margin:0}
.tile{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-4) var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-1)}
.tile__label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--on-surface-muted)}
.tile__value{font:500 32px/40px var(--font-mono)}.tile__delta{font:500 12px/16px var(--font-sans);color:var(--on-surface-variant)}
.pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:var(--radius-pill);font:500 12px/1 var(--font-sans)}
.pill--success{background:var(--success-container);color:var(--success)}.pill--warn{background:var(--warning-container);color:var(--warning)}
.pill--danger{background:var(--danger-container);color:var(--danger)}.pill--info{background:var(--info-container);color:var(--info)}.pill--neutral{background:var(--surface-container-highest);color:var(--on-surface-variant)}.pill__dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.table{width:100%;border-collapse:collapse;font-size:14px}.table th{text-align:left;color:var(--on-surface-muted);font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;padding:var(--sp-3) var(--sp-4);border-bottom:1px solid var(--outline-variant)}
.table td{padding:var(--sp-3) var(--sp-4);border-bottom:1px solid var(--outline-variant)}.table tbody tr:hover{background:rgba(228,226,230,.08)}
.appbar{background:var(--surface-container-low);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5)}
.appbar__row{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap}.appbar h1{margin:0;font:400 28px/36px var(--font-sans)}
.appbar__tenant{margin-top:var(--sp-3);color:var(--on-surface-variant);font:400 14px/20px}.appbar__tenant b{color:var(--on-surface)}
.appbar__sub{margin-top:var(--sp-2);color:var(--on-surface-muted);font:400 14px/20px;max-width:820px}.spacer{flex:1}
.section-label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--primary);margin:0}
.chips{display:flex;flex-wrap:wrap;gap:var(--sp-2)}.footer{color:var(--on-surface-muted);font:400 12px/16px;text-align:center;padding-top:var(--sp-2)}
"""
FONT = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=Roboto:wght@400;500&family=Roboto+Mono:wght@400;500&display=swap">')


def _esc(v) -> str:
    return html.escape(str(v))


def render(d: dict) -> str:
    kpis = "".join(f"<div class='tile'><div class='tile__label'>{_esc(k['label'])}</div>"
                   f"<div class='tile__value'>{_esc(k['value'])}</div>"
                   f"<div class='tile__delta'>{_esc(k['note'])}</div></div>" for k in d["kpis"])
    chips = "".join(f"<span class='pill {'pill--success' if c['configured'] else 'pill--neutral'}'>"
                    f"<span class='pill__dot'></span>{_esc(c['system'])}"
                    f"{' · live' if c['configured'] else ' · stub'}</span>" for c in d["coverage"])

    def spill(s):
        return {"fulfilled": "pill--success", "open": "pill--info",
                "awaiting_verification": "pill--warn"}.get(s, "pill--neutral")
    rows = "".join(
        "<tr>"
        f"<td style='font-family:var(--font-mono)'>{_esc(r.get('id'))}</td>"
        f"<td>{_esc(r.get('type'))}</td><td>{_esc(r.get('email'))}</td>"
        f"<td><span class='pill {spill(r.get('status'))}'>{_esc((r.get('status') or '').upper())}</span></td>"
        f"<td>{_esc((r.get('due_at') or '')[:10])}</td></tr>"
        for r in d["requests"]) or "<tr><td colspan=5>No requests yet. POST /request to file one.</td></tr>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Agentic Privacy — {_esc(d['tenant'])}</title>
{FONT}<style>{BASE_CSS}</style></head><body class="page"><div class="shell">
<header class="appbar"><div class="appbar__row"><h1>Agentic Privacy</h1>
<span class="pill pill--info"><span class="pill__dot"></span>DSAR orchestrator</span><span class="spacer"></span></div>
<div class="appbar__tenant"><b>{_esc(d['tenant'])}</b> · GDPR / CCPA / CPRA data-subject requests</div>
<div class="appbar__sub">{_esc(SUBTITLE)}</div></header>
<section class='kpi-row'>{kpis}</section>
<section class='shell' style='gap:var(--sp-4)'><div class='section-label'>System coverage</div>
<div class='card'><div class='chips'>{chips}</div></div>
<div class='section-label'>Requests &amp; SLA</div>
<div class='card'><table class='table'><thead><tr><th>Request</th><th>Type</th><th>Subject</th><th>Status</th><th>Due</th></tr></thead>
<tbody>{rows}</tbody></table></div></section>
<footer class="footer">agentic-privacy · access · erasure · opt-out · audited · redevops.io Agentic Business OS</footer>
</div></body></html>"""


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "connectors_live": [s for s, (_f, _d, cfg) in CONNECTORS.items() if cfg()]}


@app.get("/api/activity")
def api_activity() -> JSONResponse:
    return JSONResponse(activity())


# ──────── conversational agent · Context Runtime + redevops-rag + agent-harness ────────
try:
    from context_runtime.integrations.agent_console import AgentConsole as _AgentConsole, tool as _crtool
except Exception:  # noqa: BLE001
    _AgentConsole = None

_PRIVACY_PRIMER = """A privacy REQUEST is a data subject exercising a right: ACCESS (get a copy of their data), DELETE (erasure), CORRECT (fix inaccurate data), or OPT-OUT (of sale/sharing — the CCPA/CPRA "Do Not Sell or Share" right). Anyone files one on the public /intake form.

VERIFICATION comes first: a request is PENDING until the person's identity is confirmed. We never return or delete anyone's data on an unverified request. An operator (or an emailed link) promotes it to fulfilment.

The SLA is the legal clock to respond — typically 30 days (GDPR) or 45 days (CCPA/CPRA). Overdue requests are the ones past that due date.

The DATA MAP (coverage) is every personal-data system we can reach: ERPNext (customers/leads), Listmonk (subscribers), Chatwoot (contacts), Lago (billing), Umami (analytics), Zitadel (identity). A connector is "live" once its credentials are configured. Access and delete fan out across all live connectors.

Every action is written to a tamper-evident AUDIT log (a hash chain): each entry commits to the previous one, so the record can't be altered after the fact. That's your proof of compliance.

To fulfil a verified access request the agent gathers records from every connector; to fulfil a delete it removes the subject from each — both only after verification, and both audited."""


def _t_privacy_summary(_a: dict) -> dict:
    d = activity()
    k = {x["label"]: x["value"] for x in d["kpis"]}
    return {"text": f"{k.get('Open requests')} open requests · {k.get('Overdue (SLA)')} overdue · "
            f"connectors live {k.get('Connectors live')} · {k.get('Fulfilled')} fulfilled.", "data": d["kpis"]}


def _t_list_requests(_a: dict) -> dict:
    reqs = activity()["requests"]
    if not reqs:
        return {"text": "No privacy requests on file.", "data": []}
    lines = [f"{r.get('type', '?')} · {r.get('status', '?')} · {r.get('email', '—')}"
             + (f" · due {r['due_at'][:10]}" if r.get("due_at") else "") for r in reqs[:12]]
    return {"text": "\n".join(lines), "data": reqs}


def _t_data_map(_a: dict) -> dict:
    cov = activity()["coverage"]
    lines = [f"{c['system']}: {'live' if c['configured'] else 'not configured'}" for c in cov]
    return {"text": "Personal-data systems:\n" + "\n".join(lines), "data": cov}


if _AgentConsole is not None:
    _PRIVACY_CONSOLE = _AgentConsole(
        f"{TENANT} Privacy", _PRIVACY_PRIMER,
        tools=[
            _crtool("privacy_summary", "open/overdue/fulfilled privacy requests and connectors live", _t_privacy_summary),
            _crtool("list_requests", "the privacy requests on file: type, status, email, due date", _t_list_requests),
            _crtool("data_map", "which personal-data systems (connectors) are live", _t_data_map),
        ],
        suggestions=["Any overdue requests?", "Show open requests", "How do I verify a requester?", "What is the DSAR SLA?"],
        subtitle="Ask about DSAR requests, the data map, verification and SLAs.",
        allow_side_effects=[],
    )
else:
    _PRIVACY_CONSOLE = None


@app.post("/api/agent")
async def api_agent(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if _PRIVACY_CONSOLE is None:
        return JSONResponse({"intent": "help", "text": "The assistant is offline (context-runtime unavailable).", "evidence": []})
    return JSONResponse(_PRIVACY_CONSOLE.respond((body or {}).get("message", ""), principal=_req_principal(request)))


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    page = render(activity())
    if _PRIVACY_CONSOLE is not None:
        panel = "<section class='shell' style='margin-top:16px'>" + _PRIVACY_CONSOLE.panel_html("privacy") + "</section>"
        page = page.replace("<footer", panel + "<footer", 1)
    return page


@app.post("/request")
async def request_route(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return JSONResponse(handle_request(body or {}, summarize=_llm_text))


# --- public intake form ------------------------------------------------------
# A request filed here is PENDING (awaiting identity verification) — it never returns
# anyone's data. An operator (or, in production, an emailed verification link) promotes
# it to fulfilment via POST /request with verified=true. This is the page the sites'
# "Do Not Sell or Share" / "Privacy request" links point at.
INTAKE_HTML = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Privacy request — {html.escape(TENANT)}</title>
{FONT}<style>{BASE_CSS}
.form{{max-width:560px;margin:8vh auto;display:flex;flex-direction:column;gap:var(--sp-4)}}
input,select,button{{font:400 15px/1.4 var(--font-sans);padding:12px 14px;border-radius:12px;border:1px solid var(--outline-variant);background:var(--surface-container);color:var(--on-surface)}}
button{{background:var(--primary-container);color:var(--on-primary-container);border:none;cursor:pointer;font-weight:500}}
label{{font:500 13px/1.4 var(--font-sans);color:var(--on-surface-variant)}}
#msg{{margin-top:var(--sp-3);font-size:14px}}</style></head>
<body class="page"><div class="form">
<h1 style="margin:0;font:400 28px/36px var(--font-sans)">Submit a privacy request</h1>
<p class="appbar__sub" style="margin:0">{html.escape(TENANT)} — exercise your GDPR / CCPA / CPRA rights. We verify your
identity before acting and respond within {SLA_DAYS} days.</p>
<label>Your email (the subject of the request)</label>
<input id="email" type="email" placeholder="you@example.com" required>
<label>Request type</label>
<select id="type">
  <option value="access">Access — what data do you hold about me?</option>
  <option value="delete">Delete / erase my personal data</option>
  <option value="opt_out">Do Not Sell or Share my personal information</option>
  <option value="correct">Correct my data</option>
</select>
<button onclick="submitReq()">Submit request</button>
<div id="msg"></div>
</div>
<script>
async function submitReq() {{
  const email = document.getElementById('email').value.trim();
  const type = document.getElementById('type').value;
  const msg = document.getElementById('msg');
  if (!email) {{ msg.textContent = 'Please enter your email.'; return; }}
  msg.textContent = 'Submitting…';
  try {{
    const r = await fetch('/intake', {{method:'POST',headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{email, type}})}});
    const d = await r.json();
    msg.style.color = '#5bd98a';
    msg.textContent = d.message || ('Received: ' + (d.request_id||''));
  }} catch (e) {{ msg.style.color = '#f2544f'; msg.textContent = 'Something went wrong — email privacy@ instead.'; }}
}}
</script></body></html>"""


@app.get("/intake", response_class=HTMLResponse)
def intake_form() -> str:
    return INTAKE_HTML


@app.post("/intake")
async def intake_submit(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    email = (body.get("email") or "").strip().lower()
    rtype = (body.get("type") or "access").lower()
    if not email or "@" not in email:
        return JSONResponse({"status": "error", "error": "a valid email is required"}, status_code=400)
    # intake agent (core): opens the DSAR, sends the verification email, audits + persists.
    return JSONResponse(core.intake(email, rtype, str(request.base_url)))


def _verify_page(message: str, ok: bool) -> str:
    color = "#5bd98a" if ok else "#f2544f"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>Privacy request — {html.escape(TENANT)}</title>{FONT}<style>{BASE_CSS}</style></head>"
            f"<body class='page'><div class='shell' style='max-width:560px;margin:12vh auto'>"
            f"<div class='card'><h1 style='margin:0;font:400 24px/32px var(--font-sans)'>Privacy request</h1>"
            f"<p style='color:{color};font-size:15px'>{html.escape(message)}</p>"
            f"<p class='footer' style='text-align:left'>{html.escape(TENANT)} · GDPR / CCPA / CPRA</p>"
            f"</div></div></body></html>")


@app.get("/verify", response_class=HTMLResponse)
def verify(id: str = "", token: str = "") -> str:
    reqs = _load_requests()
    rec = next((r for r in reqs if r.get("id") == id), None)
    if not (rec and token and rec.get("verify_token") == token and not rec.get("verified")):
        return _verify_page("This verification link is invalid, already used, or expired.", False)
    rec["verified"] = True
    rec.pop("verify_token", None)
    rtype = rec.get("type")
    if rtype in ("access", "know"):
        results = _fan_out(rec["email"], "find")
        rec["status"] = "fulfilled"
        rec["record_count"] = sum(r.get("count", 0) for r in results if r.get("ok"))
        _audit({"req": id, "type": "access_fulfilled_after_verify", "email": rec["email"],
                "count": rec["record_count"]})
        msg = ("Identity verified. We've compiled the personal data we hold about you and will "
               "deliver it to this email securely.")
    elif rtype in ("opt_out", "opt-out", "do_not_sell"):
        rec["status"] = "fulfilled"
        _audit({"req": id, "type": "opt_out_verified", "email": rec["email"]})
        msg = "Identity verified. Your opt-out of sale/sharing is recorded."
    else:  # delete / correct → verified; erasure executes under the operator confirm gate
        rec["status"] = "verified"
        _audit({"req": id, "type": "verified", "request_type": rtype, "email": rec["email"]})
        msg = ("Identity verified. Your erasure request will be actioned and you'll receive "
               "confirmation when it's complete.")
    _save_requests(reqs)
    return _verify_page(msg, True)


@app.get("/audit/verify")
def audit_verify() -> JSONResponse:
    """Confirm the audit log's hash chain is intact (tamper-evidence)."""
    return JSONResponse(verify_audit_chain())


@app.post("/retention")
async def retention_route(request: Request) -> JSONResponse:
    """Retention enforcement: scan (dry-run) or purge (confirm:true) PII past the window."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return JSONResponse(retention_scan(confirm=bool((body or {}).get("confirm"))))


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive DSAR fulfilment as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_privacy_operator
    app.include_router(build_privacy_operator().router())
except Exception:  # noqa: BLE001 — agentic_os absent → no operator surface
    pass
