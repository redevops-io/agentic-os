"""agentic-support — the support vertical slice, wrapping a real Chatwoot core.

Same pattern as agentic-billing (the reference module): wrap the running self-hosted
**Chatwoot** instance (the OSS shared-inbox / support core) with

  * an agent layer that reads REAL Chatwoot data over its REST API, and
  * an MD3 dashboard (Zendesk/Intercom-style support layout, same design tokens as
    deploy/module_service.py) rendered from that live data — no mock data.

Endpoints:
  GET  /health        -> {"status","core":"chatwoot","connected": <bool>}
  GET  /api/activity  -> live KPIs + ticket queue derived from Chatwoot REST
  GET  /              -> MD3 support dashboard rendered from the live conversations
  POST /agent/run     -> agentic action:
                           {"action":"draft_reply","conversation_id":N}  -> posts a
                               PRIVATE NOTE (human-reviewable draft; never sent to the
                               customer without approval)
                           {"action":"resolve","conversation_id":N}      -> toggle status (operator-gated)
                           {"action":"escalate","conversation_id":N}     -> assign + urgent
                           {"action":"send_onboarding","customer":{...}} -> welcome message

Config (env; seed.py writes agents/support/.env automatically):
  CHATWOOT_API_URL     REST base, default http://localhost:3003
  CHATWOOT_API_TOKEN   agent access token (User#access_token.token from the seed),
                       sent as the `api_access_token` header
  CHATWOOT_ACCOUNT_ID  numeric account id (default 1)
  CHATWOOT_FRONT_URL   Chatwoot UI link for the "Open in Chatwoot ↗" button
  PORT                 uvicorn port, default 8207
  REDEVOPS_LLM_BASE_URL  OPTIONAL in-boundary model endpoint; draft_reply uses it via the governed router.
                       Unset => a deterministic template (no external provider is ever contacted).
  OPERATOR_BASIC_AUTH  `user:pass` operator credential; /agent/run actions that change a ticket or message a
                       customer execute only with it (else they return a staged pending_approval preview).
"""
from __future__ import annotations

import html
import os

# ── enterprise permissions plane — gate tool/data access by (app, user) ──
# Basic default without the enterprise package and without CR_PERMISSIONS / CR_IDENTITY_*; if either is set and the gate cannot
# be installed, agentic_os.app_gate refuses to start the app (it never runs ungated by accident).
from agentic_os.app_gate import RequestContext as _RequestContext, denial_result as _denial_result, deny_response as _deny_response, install_gate as _install_gate
from agentic_os.app_gate import with_authorization as _with_authorization
_GATE_APP = "support"
_req_principal = _install_gate(_GATE_APP)   # starts gated or not at all when permissions were requested (RR-01)
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# --- config ------------------------------------------------------------------
# The Chatwoot REST client, KPIs and agentic actions live in core.py (pure — no web /
# context-runtime deps) so the Mission Runtime operator can invoke them and they can be
# tested against a fake Chatwoot. Import config + core helpers from there; core loads .env.
from . import core
from .core import (  # noqa: E402
    CHATWOOT_API_URL, CHATWOOT_API_TOKEN, CHATWOOT_ACCOUNT_ID, CHATWOOT_FRONT_URL,
    TENANT, SUBTITLE, _headers, _acct_base, chatwoot_connected, _list_conversations,
    _contact_name, _first_inbound, _channel, _truncate, fetch_activity, _get_conversation,
)

PORT = int(os.environ.get("PORT", "8207"))

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


app = FastAPI(title="agentic-support (Meridian Wealth Management · core: Chatwoot)")

from agentic_os.app_kit import route_authz as _route_authz  # noqa: E402
from .route_policy import APP as _APP, ROUTE_POLICIES as _ROUTE_POLICIES  # noqa: E402
_route_authz.install(app, _APP, _ROUTE_POLICIES)   # one enforcement layer for every write route
app.add_middleware(_RequestContext)   # the active gate (basic or enterprise) sees THIS request's principal in every route


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


def _priority_pill(priority: str) -> str:
    p = (priority or "").lower()
    if p == "urgent":
        return "pill--danger"
    if p == "high":
        return "pill--warn"
    if p in ("medium", "normal"):
        return "pill--info"
    return "pill--neutral"


def _status_pill(status: str) -> str:
    s = (status or "").lower()
    if s == "resolved":
        return "pill--success"
    if s == "pending":
        return "pill--warn"
    if s == "open":
        return "pill--info"
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


def _escalation_banner(data: dict) -> str:
    """Surface the highest-priority open ticket the agent can act on."""
    urgent = [t for t in data.get("queue", []) if (t.get("priority") or "").lower() in ("urgent", "high")]
    if not urgent:
        return ""
    first = urgent[0]
    extra = f" (+{len(urgent) - 1} more)" if len(urgent) > 1 else ""
    return (
        "<div class='banner'>"
        f"<span class='pill pill--danger'><span class='pill__dot'></span>{len(urgent)} need attention</span>"
        "<span class='label-m' style='text-transform:uppercase;color:var(--warning)'>escalate / draft_reply</span>"
        f"<span class='body-m'>#{_esc(first['id'])} {_esc(first['contact'])} — “{_esc(first['subject'])}” "
        f"({_esc(first['priority'])}, {_esc(first['age'])} old){_esc(extra)}. "
        "Agent can draft a reply (private note) or escalate; a human approves any public reply.</span>"
        "</div>"
    )


def _channel_bars(data: dict) -> str:
    rows = ""
    for ch in data.get("channels", []):
        pct = int(ch["pct"])
        rows += (
            "<div class='barlist__row'>"
            f"<div class='barlist__label'>{_esc(ch['label'])}</div>"
            f"<div class='bar'><span style='width:{pct}%'></span></div>"
            f"<div class='barlist__pct'>{pct}% · {_esc(ch['count'])}</div>"
            "</div>"
        )
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Tickets by channel (live)</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from Chatwoot</span></div>"
        f"<div class='barlist'>{rows}</div>"
        "</div>"
    )


def _queue_table(data: dict) -> str:
    rows = ""
    for t in data["queue"]:
        prio = (t["priority"] or "normal")
        rows += (
            "<tr>"
            f"<td>#{_esc(t['id'])}</td>"
            f"<td>{_esc(t['contact'])}</td>"
            f"<td>{_esc(t['subject'])}</td>"
            f"<td>{_esc(t['channel'])}</td>"
            f"<td><span class='pill {_priority_pill(prio)}'>{_esc(prio.upper())}</span></td>"
            f"<td><span class='pill {_status_pill(t['status'])}'>{_esc((t['status'] or '').upper())}</span></td>"
            f"<td class='num'>{_esc(t['age'])}</td>"
            "</tr>"
        )
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Live ticket queue</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from Chatwoot</span></div>"
        "<table class='table'><thead><tr>"
        "<th>Ticket</th><th>Contact</th><th>Subject</th><th>Channel</th><th>Priority</th><th>Status</th><th>Age</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "</div>"
    )


def render(data: dict) -> str:
    connected = data["connected"]
    conn_txt = "core: Chatwoot connected" if connected else "core: Chatwoot UNREACHABLE"
    conn_cls = "pill--success" if connected else "pill--danger"
    status_pill = (
        f"<span class='pill {conn_cls}'><span class='pill__dot'></span>agent active · {_esc(conn_txt)}</span>"
    )
    live_badge = "<span class='pill pill--info'><span class='pill__dot'></span>data: live from Chatwoot</span>"
    open_btn = f"<a class='btn' href='{_esc(data['front_url'])}' target='_blank' rel='noopener'>Open in Chatwoot ↗</a>"

    body = (
        _escalation_banner(data)
        + _kpi_tiles(data["kpis"])
        + "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Support activity</div>"
        "<div class='grid'>"
        f"<div class='col-4'>{_channel_bars(data)}</div>"
        f"<div class='col-8'>{_queue_table(data)}</div>"
        "</div></section>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentic Support — {_esc(TENANT)}</title>
{FONT_LINK}
<style>{BASE_CSS}{PAGE_CSS}</style>
</head>
<body class="page">
<div class="shell">
  <header class="appbar">
    <div class="appbar__row">
      <h1>Agentic Support</h1>
      {status_pill}
      {live_badge}
      <span class="spacer"></span>
      {open_btn}
    </div>
    <div class="appbar__tenant"><b>{_esc(TENANT)}</b> · core: Chatwoot (open-source support)</div>
    <div class="appbar__sub">{_esc(SUBTITLE)}</div>
  </header>
  {body}
  <footer class="footer">agentic-support · live activity for {_esc(TENANT)} ·
    <a href="/api/activity">/api/activity</a> · agent + human, on a real Chatwoot core · redevops.io Agentic Business OS</footer>
</div>
</body>
</html>"""


# --- governed LLM (N7) -------------------------------------------------------
# Customer text goes into these prompts, so every model call routes through the GovernedModelRouter
# (agentic_os.app.llm.governed_text) as CUSTOMER_CONFIDENTIAL: it can only reach the configured IN-BOUNDARY
# endpoint (REDEVOPS_LLM_BASE_URL) and returns None -- never a public fallback -- when none is configured or
# the router refuses. Callers then degrade to deterministic text.
def _llm_text(prompt: str, max_tokens: int = 320) -> str | None:
    try:
        from agentic_os.app.llm import governed_text
        from agentic_os.governance.classification import DataClassification
    except Exception:  # noqa: BLE001
        return None
    return governed_text(prompt, classifications=(DataClassification.CUSTOMER_CONFIDENTIAL,),
                         max_tokens=max_tokens, temperature=0.4)


# --- agentic actions (thin wrappers over core) ---
# The approval gate lives INSIDE core.resolve / escalate / send_onboarding (staged unless `_approval` is present).
def _draft_reply(body: dict) -> dict:
    """Draft a reply over the real Chatwoot core, with the optional governed-LLM draft."""
    return core.draft_reply(body, blurb=_llm_text)


def _resolve(body: dict) -> dict:
    return core.resolve(body)


def _escalate(body: dict) -> dict:
    return core.escalate(body)


def _send_onboarding(body: dict) -> dict:
    return core.send_onboarding(body)


_ACTIONS = {"draft_reply": _draft_reply, "resolve": _resolve, "escalate": _escalate,
            "send_onboarding": _send_onboarding}


def _supported_actions() -> list:
    return sorted(_ACTIONS)


def _run_action(action: str, body: dict, authorization: "str | None") -> "dict | None":
    """THE single route -> core entry point. The operator marker is injected server-side only when the request
    carries the valid operator Basic credential; any other caller gets a staged preview from the core gate."""
    fn = _ACTIONS.get(action)
    if fn is not None:
        denied = _denial_result(_GATE_APP, {"draft_reply": "support.draft_reply", "resolve": "support.resolve", "escalate": "support.escalate",
                                            "send_onboarding": "support.send_onboarding"}[action])   # authorise where it executes
        if denied is not None:
            return denied
    if fn is None:
        return None
    return fn(core.with_operator_approval(body, authorization))


# ──────── conversational agent: governed LLM + read-only tools (no side-effecting tool, no operator marker) ────────

_SUPPORT_PRIMER = """A CONVERSATION is one customer thread in Chatwoot. Its STATUS moves through OPEN (needs an agent), PENDING (waiting on the customer or a third party), SNOOZED (hidden until a set time), and RESOLVED (closed). Reopening a resolved thread sets it back to open. You resolve a conversation with the Resolve button (or by toggling status); resolving is what closes the ticket.

An INBOX is a channel conversations arrive through — a website live-chat widget, an email address, or an API/social channel. Every conversation belongs to exactly one inbox, and you route by assigning inboxes to the right agents or teams.

An AGENT is a member of your team who answers tickets; a CONTACT is the customer on the other side. ASSIGNING a conversation gives one agent ownership so nothing is dropped — assign from the conversation header, and use auto-assignment on an inbox to spread load.

A REPLY is public and goes to the customer. A PRIVATE NOTE is internal-only — teammates see it, the customer never does. Use notes to hand off context or draft before you send. This agent only ever stages a draft as a private note; a human converts it to a public reply before the customer sees anything.

A CANNED RESPONSE is a saved reply you insert by typing its short code (e.g. "//refund") so you don't rewrite the same answer. Create and edit them under Settings → Canned Responses; they are the fastest way to keep tone consistent across the team.

A LABEL is a tag you attach to a conversation to categorize it (billing, bug, vip) and filter the inbox later. Labels drive reporting and let you build saved views for a topic or product area.

An SLA is your response-time promise. FIRST-RESPONSE TIME is how long the customer waits for the first agent reply; keeping it under your SLA (here 30 minutes) is the main queue-health signal. Prioritize open conversations with the oldest wait and any marked urgent/high."""


def _t_inbox_summary(_a: dict) -> dict:
    d = fetch_activity()
    c = d["counts"]
    k = {x["label"]: x["value"] for x in d["kpis"]}
    # Oldest still-waiting ticket from the live queue (queue = open + pending, urgent-first;
    # scan for the largest age to surface the one breaching SLA soonest).
    oldest = None
    for t in d.get("queue", []):
        if oldest is None or _age_secs(t.get("age")) > _age_secs(oldest.get("age")):
            oldest = t
    wait = ""
    if oldest:
        wait = (f" Oldest waiting: #{oldest['id']} {oldest['contact']} — "
                f"\"{oldest['subject']}\" ({oldest['age']} old, {oldest['priority']}).")
    return {"text": f"{c['open']} open · {c['pending']} pending · {c['resolved']} resolved "
            f"({c['total']} total). First response {k.get('First response')}, "
            f"resolution rate {k.get('Resolution rate')}.{wait}",
            "data": {"counts": c, "oldest_waiting": oldest}}


def _age_secs(age: str | None) -> int:
    """Parse a '5m'/'3h'/'2d' age label back to seconds for oldest-first sorting."""
    if not age or not age[:-1].isdigit():
        return 0
    n, unit = int(age[:-1]), age[-1]
    return n * {"m": 60, "h": 3600, "d": 86400}.get(unit, 0)


def _t_list_conversations(_a: dict) -> dict:
    q = fetch_activity().get("queue", [])
    if not q:
        return {"text": "The queue is clear — no open or pending conversations right now.", "data": []}
    lines = [f"#{t['id']} {t['contact']} ({t['channel']}, {t['status']}, {t['age']} old): \"{t['subject']}\""
             for t in q[:10]]
    return {"text": f"{len(q)} open/pending conversation(s):\n" + "\n".join(lines), "data": q}


def _t_draft_reply(a: dict) -> dict:
    """Read-only: gather context for a suggested reply and let the console LLM draft it.

    Never posts to Chatwoot. If a conversation id is given, pull the real customer
    message; otherwise draft from the free-text topic. The console narrates the reply.
    """
    conv_id = a.get("conversation") or a.get("conversation_id")
    if conv_id:
        conv = _get_conversation(int(conv_id))
        if not conv:
            return {"text": f"Conversation #{conv_id} not found — give me a topic instead and I'll draft one.",
                    "data": None}
        contact = _contact_name(conv)
        subject = _first_inbound(conv)
        channel = _channel(conv)
        return {"text": f"Draft a friendly, professional client-service reply for Meridian Wealth Management to "
                f"{contact} on #{conv_id} ({channel}). Their message: \"{subject}\". Keep it 3-5 "
                f"sentences, set a next step, do not invent specific returns, figures, or firm dates. This is a suggestion "
                f"only — a human reviews it before it reaches the customer.",
                "data": {"conversation_id": int(conv_id), "contact": contact, "subject": _truncate(subject)}}
    topic = (a.get("topic") or "").strip()
    if not topic:
        return {"text": "What should the reply be about? Give me a topic or a conversation id.", "data": None}
    return {"text": f"Draft a friendly, professional client-service reply for Meridian Wealth Management about: \"{topic}\". "
            f"Keep it 3-5 sentences, warm and concrete, set a clear next step, and do not invent specific returns, "
            f"figures, or firm dates. This is a suggested draft only — a human reviews it before it is sent.",
            "data": {"topic": topic}}




def _agent_reply(message: str) -> dict:
    """Answer a console message from READ-ONLY tools; the governed LLM only phrases/answers. Nothing here can
    mutate Chatwoot and no approval marker is ever supplied."""
    low = (message or "").lower()
    if not low.strip():
        return {"intent": "help", "text": "Ask about the queue, a conversation, or have me draft a reply.", "evidence": []}
    _need = ("draft_reply" if any(w in low for w in ("draft", "reply", "respond")) else
             "list_conversations" if any(w in low for w in ("list", "queue", "conversation", "ticket")) else
             "inbox_summary" if any(w in low for w in ("inbox", "summary", "open", "pending", "sla", "response")) else None)
    if _need and (_d := _denial_result(_GATE_APP, _need)):
        return {"intent": _need, "text": _d["error"], "evidence": [], "denied": True}
    if any(w in low for w in ("draft", "reply", "respond")):
        t = _t_draft_reply({"topic": message})
        txt = _llm_text(t["text"], 320) or ("No in-boundary model is configured, so I can't draft text; "
                                            "use the draft_reply action for a deterministic template.")
        return {"intent": "draft_reply", "text": txt, "evidence": []}
    if any(w in low for w in ("list", "queue", "conversation", "ticket")):
        t = _t_list_conversations({})
        return {"intent": "list_conversations", "text": t["text"], "evidence": []}
    if any(w in low for w in ("inbox", "summary", "open", "pending", "sla", "response")):
        t = _t_inbox_summary({})
        return {"intent": "inbox_summary", "text": t["text"], "evidence": []}
    txt = _llm_text(_SUPPORT_PRIMER + "\n\nAnswer in 2-4 plain sentences.\nQuestion: " + message, 320)
    return {"intent": "help", "text": txt or "The assistant model is offline (no in-boundary model configured).",
            "evidence": []}


_AGENT_PANEL = (
    "<section class='shell' style='margin-top:var(--sp-4)'><h3>Assistant</h3>"
    "<input id='agmsg' placeholder='Ask about the queue or a draft reply' style='width:70%'> "
    "<button onclick=\"fetch('api/agent',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({message:document.getElementById('agmsg').value})}).then(r=>r.json())"
    ".then(d=>{document.getElementById('agout').textContent=d.text})\">Ask</button>"
    "<pre id='agout' style='white-space:pre-wrap'></pre></section>")


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return _with_authorization(_GATE_APP, {"status": "ok", "core": "chatwoot", "connected": chatwoot_connected()})


@app.post("/api/agent")
async def api_agent(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        body = {}
    return JSONResponse(_agent_reply((body or {}).get("message", "")))


@app.get("/api/activity")
def activity() -> JSONResponse:
    if (_r := _deny_response(_GATE_APP, "inbox_summary")):
        return _r
    return JSONResponse(fetch_activity())


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    if (_r := _deny_response(_GATE_APP, "inbox_summary")):
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
    if res is not None:
        return JSONResponse(res, status_code=403 if res.get("status") == "denied" and res.get("executed") is False else 200)
    return JSONResponse(
        {"status": "error", "error": f"unknown action '{action}'",
         "supported": _supported_actions()},
        status_code=400,
    )



if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    from agentic_os.app_gate import require_explicit_setting_when_exposed as _exposed

    _exposed("0.0.0.0")   # a reachable bind needs an explicit permissions setting

    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive support as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_support_operator
    app.include_router(build_support_operator().router())
    # Runtime-native: register this app's governed contract at boot (guarded with the mount above).
    from agentic_os.app_kit.boot import register_app_manifest as _register_runtime_native
    from . import manifest as _runtime_native_manifest
    _register_runtime_native(_runtime_native_manifest)
except Exception as _op_exc:  # agentic_os absent / operator build failed — surface it, do not hide it
    import logging as _logging
    _logging.getLogger("agentic_os.app").error(
        "%s: operator/capability surface NOT mounted (%s: %s) — /invoke + /capabilities are unavailable",
        __name__, type(_op_exc).__name__, _op_exc)
