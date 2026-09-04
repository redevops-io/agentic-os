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
                           {"action":"resolve","conversation_id":N}      -> toggle status
                           {"action":"escalate","conversation_id":N}     -> assign + urgent
                           {"action":"send_onboarding","customer":{...}} -> welcome message

Config (env; seed.py writes agents/support/.env automatically):
  CHATWOOT_API_URL     REST base, default http://localhost:3003
  CHATWOOT_API_TOKEN   agent access token (User#access_token.token from the seed),
                       sent as the `api_access_token` header
  CHATWOOT_ACCOUNT_ID  numeric account id (default 1)
  CHATWOOT_FRONT_URL   Chatwoot UI link for the "Open in Chatwoot ↗" button
  PORT                 uvicorn port, default 8207
  ANTHROPIC_API_KEY    OPTIONAL — if set, draft_reply uses Claude to write the draft;
                       a deterministic template fallback runs without it.
"""
from __future__ import annotations

import html
import os

# ── enterprise permissions plane — gate tool/data access by (app, user) ──
# No-op without the context_runtime_enterprise package or CR_PERMISSIONS=1 (open-core default).
try:
    from context_runtime_enterprise.apps import bootstrap as _cr_bootstrap
    _req_principal = _cr_bootstrap("support")
except Exception:  # noqa: BLE001
    def _req_principal(request=None):
        return None
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

app = FastAPI(title="agentic-support (Meridian Wealth Management · core: Chatwoot)")


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


# --- optional LLM reasoning (guarded: works without any API key) -------------
# Narration runs on the Context Runtime model plane (on-prem DeepSeek via REDEVOPS_LLM_*,
# or a configured provider) — same stack as the assistant, no raw provider SDK.
try:
    from context_runtime.adapters.model_openai import OpenAICompatibleModel as _BlurbModelCls
    from context_runtime.types import ModelRequest as _BlurbReq
    _BLURB_MODEL = _BlurbModelCls.from_env()
except Exception:  # noqa: BLE001 — context-runtime absent → narration off
    _BLURB_MODEL = None
    _BlurbReq = None


def _llm_text(prompt: str, max_tokens: int = 320) -> str | None:
    """Return narration text via the Context Runtime model plane, or None (offline / any error).

    Optional by design — draft_reply has a deterministic template fallback so the absence of a
    model never breaks the endpoint.
    """
    if _BLURB_MODEL is None or _BlurbReq is None:
        return None
    try:
        res = _BLURB_MODEL.complete(_BlurbReq(messages=({"role": "user", "content": prompt},), max_tokens=max_tokens))
        return res.text or None
    except Exception:  # noqa: BLE001
        return None


# --- agentic actions (thin wrappers over core; core stays context-runtime-free) ---
def _draft_reply(body: dict) -> dict:
    """Draft a reply over the real Chatwoot core, with the optional LLM draft blurb."""
    return core.draft_reply(body, blurb=_llm_text)


def _resolve(body: dict) -> dict:
    """Toggle a conversation to resolved via the real Chatwoot core."""
    return core.resolve(body)


def _escalate(body: dict) -> dict:
    """Set priority urgent + assign via the real Chatwoot core."""
    return core.escalate(body)


def _send_onboarding(body: dict) -> dict:
    """Send a welcome message to a newly-onboarded customer via the real Chatwoot core."""
    return core.send_onboarding(body)


# ──────── conversational agent · Context Runtime (LLM) + redevops-rag (grounding) + agent-harness (tools) ────────
try:
    from context_runtime.integrations.agent_console import AgentConsole as _AgentConsole, tool as _crtool
except Exception:  # noqa: BLE001 — context-runtime absent → no console
    _AgentConsole = None

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
            f"({c['total']} total). First response {k.get('First response')}, CSAT {k.get('CSAT')}.{wait}",
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


if _AgentConsole is not None:
    _SUPPORT_CONSOLE = _AgentConsole(
        f"{TENANT} Support", _SUPPORT_PRIMER,
        tools=[
            _crtool("inbox_summary", "queue health — open/pending/resolved counts, first-response, and the oldest waiting ticket", _t_inbox_summary),
            _crtool("list_conversations", "list recent open/pending conversations with contact, channel and message snippet", _t_list_conversations),
            _crtool("draft_reply", "gather context and suggest a support reply for a topic or a conversation id (read-only — never sent)", _t_draft_reply,
                    parameters={"type": "object", "properties": {"topic": {"type": "string"}, "conversation": {"type": "string"}}}),
        ],
        suggestions=["What's in my inbox?", "Draft a reply about a refund", "How do I use canned responses?", "What does pending mean?"],
        subtitle="Ask how Chatwoot works, check the queue, or have me draft a reply — a human sends anything public.",
        allow_side_effects=[],  # everything here is read-only; no auto-send, no auto-resolve
    )
else:
    _SUPPORT_CONSOLE = None


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "core": "chatwoot", "connected": chatwoot_connected()}


@app.post("/api/agent")
async def api_agent(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if _SUPPORT_CONSOLE is None:
        return JSONResponse({"intent": "help", "text": "The assistant is offline (context-runtime unavailable).", "evidence": []})
    return JSONResponse(_SUPPORT_CONSOLE.respond((body or {}).get("message", ""), principal=_req_principal(request)))


@app.get("/api/activity")
def activity() -> JSONResponse:
    return JSONResponse(fetch_activity())


# --- Context Runtime: live decisions over a synthetic ticket stream ----------
import asyncio as _cr_asyncio
import json as _cr_json
from datetime import datetime as _cr_dt, timezone as _cr_tz
from fastapi.responses import StreamingResponse as _CRStreamingResponse

try:
    from context_runtime.integrations.agentic_support import (  # type: ignore
        AgenticSupportTenant as _CRTenant, agentic_support_bucket as _cr_bucket,
    )
    _CR = _CRTenant(epsilon=0.15)
except Exception:  # noqa: BLE001
    _CR = None

    def _cr_bucket(_t):  # type: ignore
        return "general"

_CR_SYNTH = [
    'How do I configure SSO?',
    'Export throws a 500 error',
    'Why was I charged twice?',
    'Dashboard is down and unavailable',
]


def _cr_decide(text: str) -> dict:
    try:
        bucket = _cr_bucket(text)
    except Exception:  # noqa: BLE001
        bucket = "general"
    if _CR is not None:
        try:
            try:
                arm = _CR.choose(text, bucket=bucket)
            except TypeError:
                arm = _CR.choose(text)
            try:
                _CR.record_outcome(text, 5.0)
            except Exception:  # noqa: BLE001
                pass
            return {"bucket": str(bucket), "bundle": getattr(arm, "key", str(arm))}
        except Exception:  # noqa: BLE001
            pass
    return {"bucket": str(bucket), "bundle": "(context runtime offline)"}

_CR_LIVE_FEED = """
<div id="cr-live" style="position:fixed;right:16px;bottom:16px;width:340px;max-height:58vh;overflow:auto;background:#17171a;border:1px solid #2f2f33;border-radius:12px;padding:12px;font:13px/1.45 Roboto,system-ui,sans-serif;color:#e4e2e6;z-index:9999;box-shadow:0 10px 34px rgba(0,0,0,.45)">
  <div style="color:#4fd1c5;font-weight:600;margin-bottom:8px">Context Runtime — live decisions</div>
  <div id="cr-feed" style="color:#9b99a1">connecting…</div>
</div>
<script>
(function(){
  var feed=document.getElementById('cr-feed');var first=true;
  try{
    var es=new EventSource('api/stream');
    es.onmessage=function(e){
      if(first){feed.innerHTML='';first=false;}
      var d=JSON.parse(e.data);var row=document.createElement('div');
      row.style.cssText='border-top:1px solid #2f2f33;padding:7px 0';
      row.innerHTML='<div style="color:#9b99a1;font-size:11px">'+d.ts+' \u00b7 <b style="color:#c7c5ca">'+d.bucket+'</b></div>'+'<div style="margin:2px 0">'+d.input+'</div>'+'<div style="color:#4fd1c5">\u2192 pulled context: <b>'+d.bundle+'</b></div>';
      feed.insertBefore(row,feed.firstChild);
      while(feed.children.length>8) feed.removeChild(feed.lastChild);
    };
    es.onerror=function(){ if(first){feed.textContent='(live stream unavailable)';} };
  }catch(err){feed.textContent='(live stream unavailable)';}
})();
</script>
"""


@app.get("/api/stream")
async def cr_stream() -> _CRStreamingResponse:
    async def _gen():
        i = 0
        while True:
            text = _CR_SYNTH[i % len(_CR_SYNTH)]
            i += 1
            d = _cr_decide(text)
            evt = {"input": text, "ts": _cr_dt.now(_cr_tz.utc).strftime("%H:%M:%S"), **d}
            yield f"data: {_cr_json.dumps(evt)}\n\n"
            await _cr_asyncio.sleep(2.5)
    return _CRStreamingResponse(_gen(), media_type="text/event-stream")


_CR_BANNER = """<div style="position:sticky;top:0;z-index:9998;background:linear-gradient(90deg,#10201d,#17171a);border-bottom:1px solid #2f2f33;color:#e4e2e6;font:13px/1.4 Roboto,system-ui,sans-serif;padding:9px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap"><span style="background:#4fd1c5;color:#08110f;font-weight:700;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">CONTEXT RUNTIME</span><span style="background:#2f2f33;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">DEMO</span><span style="color:#9b99a1">This demo app is plugged into <b style="color:#e4e2e6">Context Runtime</b>, which optimizes which ticket context to retrieve — resolution quality vs retrieval cost (3.68 vs 2.39). <a href="https://github.com/redevops-io/context-runtime" style="color:#4fd1c5;text-decoration:none">learn more \u2192</a></span></div>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    import re as _cr_re
    page = render(fetch_activity())
    if _SUPPORT_CONSOLE is not None:
        panel = "<section class='shell' style='margin-top:var(--sp-4)'>" + _SUPPORT_CONSOLE.panel_html("support") + "</section>"
        page = page.replace("<footer", panel + "<footer", 1)
    page = _cr_re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + _CR_BANNER, page, count=1)
    if "_CR_BANNER" not in page and "cr-live" not in page:  # no <body> matched → prepend
        page = _CR_BANNER + page
    return (page.replace("</body>", _CR_LIVE_FEED + "</body>")
            if "</body>" in page else page + _CR_LIVE_FEED)


@app.post("/agent/run")
async def agent_run(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body or {}).get("action", "")

    if action == "draft_reply":
        return JSONResponse(_draft_reply(body or {}))
    if action == "resolve":
        return JSONResponse(_resolve(body or {}))
    if action == "escalate":
        return JSONResponse(_escalate(body or {}))
    if action == "send_onboarding":
        return JSONResponse(_send_onboarding(body or {}))
    return JSONResponse(
        {"status": "error", "error": f"unknown action '{action}'",
         "supported": ["draft_reply", "resolve", "escalate", "send_onboarding"]},
        status_code=400,
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive support as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_support_operator
    app.include_router(build_support_operator().router())
except Exception:  # noqa: BLE001 — agentic_os absent → no operator surface
    pass
