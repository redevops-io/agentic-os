"""growth-assistant — the AI marketing/growth strategist for first-time founders.

The open, self-hosted answer to growth agencies (Growth Division, GrowthRocks,
Demand Curve, founder-led ghostwriting shops): instead of selling generic
"social posts", it produces *strategic traction* assets across four pillars and
wires them into the rest of the Agentic OS.

  POST /agent/run
    {"action":"playbook","startup":{...}}        -> full 4-pillar growth playbook
    {"action":"subreddit_plan","startup":{...}}  -> brand subreddit + first-100-threads
    {"action":"founder_content","startup":{...},"platform":"x|linkedin","count":7}
                                                 -> founder ghostwriting (voice + posts);
                                                    push=true best-effort drafts to Postiz
    {"action":"community_blueprint","startup":{...},"platform":"discord|whatsapp|facebook"}
                                                 -> lead-magnet community blueprint;
                                                    push=true creates a Listmonk list
    {"action":"cold_outreach","startup":{...},"targets":[{...}]}
                                                 -> audit Loom scripts + accelerator pitch;
                                                    push=true creates ERPNext Leads
    {"action":"hire_brief","role":"reddit_specialist|copywriter|designer","startup":{...}}
                                                 -> JD + vetting scorecard + freelancer
                                                    search links + outreach DM
    {"action":"ask","q":"..."}                   -> NL question over saved assets + cores

`startup` = {name, product, icp, stage, problem, founder_handle, links}. Every
asset is saved to GROWTH_DATA_DIR; the dashboard renders the live asset store.
Nothing is auto-published to a prospect/community without a human pushing it.

Config (env):
  PORT                       uvicorn port, default 8213
  REDEVOPS_LLM_BASE_URL / REDEVOPS_LLM_MODEL   agent brain (DeepSeek-V4-Flash)
  ANTHROPIC_API_KEY          OPTIONAL Claude fallback brain
  GROWTH_DATA_DIR            asset store, default /data
  GROWTH_TENANT              display name
  ERPNEXT_URL/_API_KEY/_API_SECRET             create first-customer Leads (CRM)
  LISTMONK_API_URL/_API_USER/_API_TOKEN        create the community list
  POSTIZ_API_URL/_API_KEY/_ORG_ID              best-effort founder-post drafts
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# --- config ------------------------------------------------------------------
# The asset store, the multi-core (ERPNext / Listmonk / Postiz) clients, the KPIs and the
# deterministic action wrappers live in core.py (pure — no web / context-runtime deps) so
# the Mission Runtime operator can invoke them and they can be tested against fake cores.
# core.py loads .env; import config + helpers from it. The LLM strategy generation (below)
# stays here and is injected into the core actions as an optional `gen` callback.
from . import core
from .core import (  # noqa: E402
    TENANT, SUBTITLE, ASSET_DIR,
    _sid, _load_assets, erp_connected, listmonk_connected, postiz_reachable, fetch_activity,
)

PORT = int(os.environ.get("PORT", "8213"))

# Chat brain: PREFER the Qwen reasoning model (CHAT_LLM_*; e.g. qwen-reasoning on
# its NodePort) when it's up, fall back to the always-on DeepSeek-V4-Flash
# (REDEVOPS_LLM_*), then Claude. qwen-reasoning is GPU/CPU and often scaled to 0,
# so a fast connect-timeout makes the fallback instant.
CHAT_LLM_BASE_URL = os.environ.get("CHAT_LLM_BASE_URL", "").rstrip("/")
CHAT_LLM_MODEL = os.environ.get("CHAT_LLM_MODEL", "qwen-reasoning")
CHAT_RATE_PER_MIN = int(os.environ.get("CHAT_RATE_PER_MIN", "8"))

app = FastAPI(title=f"growth-assistant ({TENANT})")


# --- the agent brain (DeepSeek-V4-Flash; Claude fallback) --------------------
def _llm_text(prompt: str, max_tokens: int = 900, temperature: float = 0.5) -> str | None:
    base = os.environ.get("REDEVOPS_LLM_BASE_URL")
    if base:
        try:
            r = httpx.post(base.rstrip("/") + "/chat/completions",
                           json={"model": os.environ.get("REDEVOPS_LLM_MODEL", "DeepSeek-V4-Flash"),
                                 "messages": [{"role": "user", "content": prompt}],
                                 "max_tokens": max_tokens, "temperature": temperature},
                           timeout=180.0)
            if r.status_code == 200:
                txt = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                if txt:
                    return txt
        except Exception:
            pass
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages",
                       headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                "content-type": "application/json"},
                       json={"model": "claude-opus-4-8", "max_tokens": max_tokens,
                             "messages": [{"role": "user", "content": prompt}]},
                       timeout=60.0)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])
                       if b.get("type") == "text").strip() or None
    except Exception:
        return None


def _llm_json(prompt: str, max_tokens: int = 1500) -> dict | list | None:
    """Ask for STRICT JSON and parse the first {...} / [...] block."""
    out = _llm_text(prompt + "\n\nReturn STRICT, valid JSON only — no prose, no markdown fences.",
                    max_tokens=max_tokens, temperature=0.4)
    if not out:
        return None
    for op, cl in (("{", "}"), ("[", "]")):
        i, j = out.find(op), out.rfind(cl)
        if i != -1 and j > i:
            try:
                return json.loads(out[i:j + 1])
            except Exception:
                continue
    return None


def _chat_brain(messages: list[dict], max_tokens: int = 700, temperature: float = 0.4):
    """Chat completion that PREFERS qwen-reasoning (CHAT_LLM_*), falls back to the
    always-on DeepSeek brain (REDEVOPS_LLM_*), then Claude. A short connect-timeout
    makes the fallback instant when qwen-reasoning is scaled to 0. Returns (text, brain)."""
    candidates = []
    if CHAT_LLM_BASE_URL:
        candidates.append((CHAT_LLM_BASE_URL, CHAT_LLM_MODEL, "qwen-reasoning"))
    base = os.environ.get("REDEVOPS_LLM_BASE_URL")
    if base:
        candidates.append((base.rstrip("/"),
                           os.environ.get("REDEVOPS_LLM_MODEL", "DeepSeek-V4-Flash"), "deepseek-v4-flash"))
    for url, model, label in candidates:
        try:
            r = httpx.post(url + "/chat/completions",
                           json={"model": model, "messages": messages,
                                 "max_tokens": max_tokens, "temperature": temperature},
                           timeout=httpx.Timeout(200.0, connect=4.0))
            if r.status_code == 200:
                txt = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                if txt:
                    return txt, label
        except Exception:
            continue
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        try:
            prompt = "\n\n".join(m.get("content", "") for m in messages)
            r = httpx.post("https://api.anthropic.com/v1/messages",
                           headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                    "content-type": "application/json"},
                           json={"model": "claude-opus-4-8", "max_tokens": max_tokens,
                                 "messages": [{"role": "user", "content": prompt}]}, timeout=60.0)
            r.raise_for_status()
            txt = "".join(b.get("text", "") for b in r.json().get("content", [])
                          if b.get("type") == "text").strip()
            if txt:
                return txt, "claude"
        except Exception:
            pass
    return None, "none"


# in-memory per-IP sliding-window rate limiter for the public chat surface
_RATE: dict[str, list[float]] = {}


def _rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _RATE.get(ip or "?", []) if now - t < 60.0]
    if len(hits) >= CHAT_RATE_PER_MIN:
        _RATE[ip or "?"] = hits
        return False
    hits.append(now)
    _RATE[ip or "?"] = hits
    return True


# --- LLM strategy generation (injected into the core actions as the `gen` callback) ---
# core.py's actions call `gen(prompt, max_tokens=N)`; these two adapters bind that to the
# app's DeepSeek/Claude brain. With no brain reachable they return None, and core falls
# back to a deterministic template (so /agent/run degrades gracefully, exactly as before).
def _gen_json(prompt: str, max_tokens: int = 1500):
    return _llm_json(prompt, max_tokens=max_tokens)


def _gen_text(prompt: str, max_tokens: int = 500):
    return _llm_text(prompt, max_tokens=max_tokens)


# --- MD3 dashboard (shared house tokens) -------------------------------------
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
.col-4{grid-column:span 4}.col-6{grid-column:span 6}.col-8{grid-column:span 8}.col-12{grid-column:span 12}
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
.appbar{background:var(--surface-container-low);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5)}
.appbar__row{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap}.appbar h1{margin:0;font:400 28px/36px var(--font-sans)}
.appbar__tenant{margin-top:var(--sp-3);color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}.appbar__tenant b{color:var(--on-surface)}
.appbar__sub{margin-top:var(--sp-2);color:var(--on-surface-muted);font:400 14px/20px var(--font-sans);max-width:880px}
.spacer{flex:1}a{color:var(--primary);text-decoration:none}
.btn{display:inline-flex;align-items:center;gap:6px;height:36px;padding:0 16px;border-radius:var(--radius-pill);background:var(--primary-container);color:var(--on-primary-container);font:500 14px/1 var(--font-sans);border:none}
.section-label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--primary);margin:0}
.pillar{display:flex;flex-direction:column;gap:var(--sp-2)}
.pillar h3{margin:0;font:500 15px/22px var(--font-sans)}
.pillar p{margin:0;color:var(--on-surface-muted);font:400 13px/19px var(--font-sans)}
.pillar code{font-family:var(--font-mono);font-size:12px;color:var(--on-primary-container);background:var(--primary-container);padding:1px 6px;border-radius:6px}
.footer{color:var(--on-surface-muted);font:400 12px/16px var(--font-sans);text-align:center;padding-top:var(--sp-2)}
.chatlog{display:flex;flex-direction:column;gap:var(--sp-3);max-height:360px;overflow-y:auto;padding-right:var(--sp-2);min-height:60px}
.msg{padding:var(--sp-3) var(--sp-4);border-radius:var(--radius-md);font:400 14px/20px var(--font-sans);max-width:86%;white-space:normal}
.msg--user{align-self:flex-end;background:var(--primary-container);color:var(--on-primary-container)}
.msg--bot{align-self:flex-start;background:var(--surface-container-highest);color:var(--on-surface)}
.msg__asset{display:block;margin-top:6px;color:var(--primary);font-size:13px}
.chatform{display:flex;gap:var(--sp-3);margin-top:var(--sp-2)}
.chatin{flex:1;height:40px;padding:0 var(--sp-4);border-radius:var(--radius-pill);border:1px solid var(--outline-variant);background:var(--surface-container-low);color:var(--on-surface);font:400 14px/1 var(--font-sans)}
.chatin:focus{outline:none;border-color:var(--primary)}
.chathint{color:var(--on-surface-muted);font:400 12px/16px var(--font-sans)}
"""
FONT_LINK = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
             'family=Roboto:wght@400;500&family=Roboto+Mono:wght@400;500&display=swap">')


def _esc(v) -> str:
    return html.escape(str(v))


def _kpi_tiles(kpis: list[dict]) -> str:
    cells = "".join(
        "<div class='tile'>"
        f"<div class='tile__label'>{_esc(k['label'])}</div>"
        f"<div class='tile__value'>{_esc(k['value'])}</div>"
        f"<div class='tile__delta'>{_esc(k['note'])}</div></div>"
        for k in kpis)
    return f"<section class='kpi-row'>{cells}</section>"


PILLARS = [
    ("Subreddit incubation", "Brand subreddit + first-100-threads + parallel-sub outreach.",
     "action=subreddit_plan"),
    ("Founder-led growth", "Ghostwritten X / LinkedIn in the founder's voice.",
     "action=founder_content"),
    ("Lead-magnet community", "Discord / WhatsApp / FB group around the problem.",
     "action=community_blueprint"),
    ("Cold outreach + accelerators", "Audit Looms for PH launchers + workshop-for-referrals.",
     "action=cold_outreach"),
]


def _pillar_cards() -> str:
    cards = "".join(
        "<div class='col-6'><div class='card pillar'>"
        f"<h3>{_esc(t)}</h3><p>{_esc(d)}</p><p><code>{_esc(a)}</code></p></div></div>"
        for t, d, a in PILLARS)
    return ("<section class='shell' style='gap:var(--sp-4)'>"
            "<div class='section-label'>Four traction pillars</div>"
            f"<div class='grid'>{cards}</div></section>")


def _recent_table(data: dict) -> str:
    rows = "".join(
        "<tr>"
        f"<td><span class='pill pill--neutral'>{_esc(t['type'])}</span></td>"
        f"<td>{_esc(t['title'])}</td>"
        f"<td>{_esc(t['startup'])}</td>"
        f"<td>{_esc(t['pushed'])}</td>"
        f"<td>{_esc(t['created'][:16].replace('T',' '))}</td></tr>"
        for t in data.get("recent", []))
    return ("<div class='card'><div class='card__head'>"
            "<h2 class='card__title'>Generated growth assets</h2>"
            "<span class='pill pill--info'><span class='pill__dot'></span>live from asset store</span></div>"
            "<table class='table'><thead><tr><th>Type</th><th>Title</th><th>Startup</th><th>Pushed</th><th>Created</th></tr></thead>"
            f"<tbody>{rows or '<tr><td colspan=5>No assets yet — POST /agent/run to generate.</td></tr>'}</tbody></table></div>")


def _chat_panel() -> str:
    # Fetches the RELATIVE path "api/chat" so it resolves to /api/chat standalone
    # and /m/growth-assistant/api/chat behind the control-plane proxy.
    return (
        "<div class='card' id='chatcard'>"
        "<div class='card__head'><h2 class='card__title'>Chat with the strategist</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>generation-only · rate-limited</span></div>"
        "<div id='chatlog' class='chatlog'>"
        "<div class='msg msg--bot'>Hi — tell me about your startup (name, what it does, who it's for) and "
        "what you want: a subreddit plan, founder X/LinkedIn content, a community blueprint, a cold-outreach "
        "kit, or help hiring a freelancer.</div></div>"
        "<form id='chatform' class='chatform' onsubmit='return gaSend(event)'>"
        "<input id='chatin' class='chatin' autocomplete='off' "
        "placeholder='e.g. Build a subreddit plan for my CI-insights tool for indie devs'>"
        "<button class='btn' type='submit' id='chatbtn'>Send</button></form>"
        "<div class='chathint'>Generations can take ~1–2 min (the agent runs a reasoning model).</div>"
        "<script>"
        "const GA_HIST=[];"
        "function gaSid(){var s=localStorage.getItem('ga_sid');if(!s){s=(Date.now().toString(36)+"
        "Math.random().toString(36).slice(2,10)).replace(/[^a-z0-9]/g,'');localStorage.setItem('ga_sid',s);}return s;}"
        "function gaEsc(t){return (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\\n/g,'<br>');}"
        "function gaAsset(d,asset){if(asset){var a=document.createElement('a');"
        "a.href='api/assets/'+asset+'?session='+gaSid();"
        "a.target='_blank';a.className='msg__asset';a.textContent='\\u2197 view generated asset';d.appendChild(a);}}"
        "function gaAdd(role,text,asset){var l=document.getElementById('chatlog');"
        "var d=document.createElement('div');d.className='msg msg--'+role;d.innerHTML=gaEsc(text);"
        "gaAsset(d,asset);l.appendChild(d);l.scrollTop=l.scrollHeight;return d;}"
        "async function gaJson(r){var t=await r.text();try{return JSON.parse(t);}catch(e){"
        "return {status:'error',reply:'The agent is taking too long or is unavailable \\u2014 please try again.'};}}"
        "async function gaPoll(id,div,tries){if(tries<=0){"
        "div.innerHTML=gaEsc('\\u231b Still working \\u2014 it will appear in the asset store below shortly.');return;}"
        "try{var r=await fetch('api/chat/job/'+id);var d=await gaJson(r);"
        "if(d.status==='done'){div.innerHTML=gaEsc(d.reply||'Done.');gaAsset(div,d.asset_id);"
        "GA_HIST.push({role:'assistant',content:d.reply||'Done.'});gaLoadData();return;}"
        "if(d.status==='error'){div.innerHTML=gaEsc(d.reply||'Failed.');return;}}catch(e){}"
        "setTimeout(function(){gaPoll(id,div,tries-1);},5000);}"
        "async function gaSend(e){e.preventDefault();var inp=document.getElementById('chatin');"
        "var btn=document.getElementById('chatbtn');var msg=inp.value.trim();if(!msg)return false;"
        "gaAdd('user',msg);GA_HIST.push({role:'user',content:msg});inp.value='';btn.disabled=true;"
        "var wait=gaAdd('bot','\\u2026thinking');"
        "try{var r=await fetch('api/chat',{method:'POST',headers:{'content-type':'application/json'},"
        "body:JSON.stringify({message:msg,history:GA_HIST,session:gaSid()})});var d=await gaJson(r);"
        "var reply=d.reply||d.error||'(no reply)';wait.innerHTML=gaEsc(reply);"
        "if(d.job_id){gaPoll(d.job_id,wait,48);}"
        "else{gaAsset(wait,d.asset_id);GA_HIST.push({role:'assistant',content:reply});}}"
        "catch(err){wait.innerHTML=gaEsc('\\u26a0\\ufe0f '+err);}"
        "btn.disabled=false;inp.focus();return false;}"
        "</script></div>")


def render(data: dict) -> str:
    cores = data["cores"]
    core_pills = "".join(
        f"<span class='pill {'pill--success' if v else 'pill--danger'}'><span class='pill__dot'></span>"
        f"{name}: {'connected' if v else 'down'}</span>"
        for name, v in (("ERPNext", cores["erpnext"]), ("Listmonk", cores["listmonk"]),
                        ("Postiz", cores["postiz"])))
    # KPI tiles + the asset table are rendered CLIENT-SIDE, filtered by the viewer's
    # localStorage session id, so each visitor sees only their own generations.
    assets_card = ("<div class='card'><div class='card__head'>"
                   "<h2 class='card__title'>Your generated assets</h2>"
                   "<span class='pill pill--info'><span class='pill__dot'></span>private to your session</span></div>"
                   "<table class='table'><thead><tr><th>Type</th><th>Title</th><th>Startup</th>"
                   "<th>Pushed</th><th>Created</th></tr></thead>"
                   "<tbody id='ga-assets-body'><tr><td colspan='5'>Loading…</td></tr></tbody></table></div>")
    data_script = """<script>
async function gaLoadData(){try{
  var r=await fetch('api/activity?session='+gaSid());var d=await r.json();
  var k=document.getElementById('ga-kpis');
  if(k){k.innerHTML=(d.kpis||[]).map(function(t){return "<div class='tile'><div class='tile__label'>"+gaEsc(t.label)+"</div><div class='tile__value'>"+gaEsc(t.value)+"</div><div class='tile__delta'>"+gaEsc(t.note)+"</div></div>";}).join('');}
  var b=document.getElementById('ga-assets-body');
  if(b){var rows=(d.recent||[]).map(function(t){return "<tr><td><span class='pill pill--neutral'>"+gaEsc(t.type)+"</span></td><td><a target='_blank' href='api/assets/"+t.id+"?session="+gaSid()+"'>"+gaEsc(t.title)+"</a></td><td>"+gaEsc(t.startup)+"</td><td>"+gaEsc(t.pushed)+"</td><td>"+gaEsc((t.created||'').slice(0,16).replace('T',' '))+"</td></tr>";}).join('');
  b.innerHTML=rows||"<tr><td colspan='5'>No assets yet — chat with the agent above to generate one.</td></tr>";}
}catch(e){}}
gaLoadData();
</script>"""
    body = ("<section class='kpi-row' id='ga-kpis'></section>"
            + "<section class='shell' style='gap:var(--sp-4)'>"
            "<div class='section-label'>Talk to the agent</div>"
            f"{_chat_panel()}</section>"
            + _pillar_cards()
            + "<section class='shell' style='gap:var(--sp-4)'>"
            "<div class='section-label'>Your asset store</div>"
            f"{assets_card}</section>"
            + data_script)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Growth Assistant — {_esc(TENANT)}</title>{FONT_LINK}<style>{BASE_CSS}</style></head>
<body class="page"><div class="shell">
<header class="appbar"><div class="appbar__row"><h1>Growth Assistant</h1>
<span class="pill pill--info"><span class="pill__dot"></span>AI growth strategist</span>
{core_pills}<span class="spacer"></span></div>
<div class="appbar__tenant"><b>{_esc(TENANT)}</b> · founder-led traction · vetted-freelancer sourcing</div>
<div class="appbar__sub">{_esc(SUBTITLE)}</div></header>{body}
<footer class="footer">growth-assistant ·
<a href="/api/activity">/api/activity</a> · <a href="/api/assets">/api/assets</a> ·
playbook · subreddit · founder content · community · cold outreach · hire · ask ·
redevops.io Agentic Business OS</footer>
</div></body></html>"""


# --- conversational chat layer (router + Q&A fallback) -----------------------
# The real actions live in core.py (pure, testable, operator-drivable). Here we bind
# each to the LLM `gen` callback so /agent/run and the chat layer generate real strategy;
# the Mission Runtime operator invokes the same core functions with gen=None (templates).
ACTIONS = {
    "playbook": lambda b: core.playbook(b, gen=_gen_json),
    "subreddit_plan": lambda b: core.subreddit_plan(b, gen=_gen_json),
    "founder_content": lambda b: core.founder_content(b, gen=_gen_json),
    "community_blueprint": lambda b: core.community_blueprint(b, gen=_gen_json),
    "cold_outreach": lambda b: core.cold_outreach(b, gen=_gen_json),
    "hire_brief": lambda b: core.hire_brief(b, gen=_gen_json),
    "ask": lambda b: core.ask(b, gen=_gen_text),
}

_CHAT_SYSTEM = (
    "You are the Growth Assistant — an AI growth strategist for first-time founders "
    "(subreddit incubation, founder-led X/LinkedIn growth, lead-magnet communities, cold "
    "outreach, and hiring vetted freelancers). From the conversation, decide whether to "
    "TRIGGER A GENERATION ACTION or REPLY conversationally.\n"
    "Triggerable actions (and their required params):\n"
    "  playbook · subreddit_plan · cold_outreach (need a startup name + some context)\n"
    "  founder_content (params.platform = x|linkedin) · community_blueprint "
    "(params.platform = discord|whatsapp|facebook) · hire_brief (params.role = "
    "reddit_specialist|copywriter|designer)\n"
    "Respond with STRICT JSON ONLY, no prose, no fences:\n"
    '{"mode":"action|chat","action":"<action or empty>",'
    '"startup":{"name":"","product":"","icp":"","stage":"","problem":"","founder_handle":""},'
    '"params":{"platform":"","count":7,"role":""},'
    '"reply":"<short friendly reply: if action, say what you are generating; if chat, '
    'answer OR ask for the one missing detail>"}\n'
    "Use mode=action ONLY when the user clearly wants something generated AND you have a "
    "startup name plus enough context. Otherwise mode=chat."
)


# Background job store — generation actions run async so each HTTP request stays
# well under Cloudflare's ~100s edge timeout (a synchronous ~96-138s generation
# would 524 and break the chat). The client kicks off a job, then polls.
_JOBS: dict[str, dict] = {}


def _run_action_job(job_id: str, action: str, call: dict) -> None:
    try:
        result = ACTIONS[action](call)
        prior = _JOBS.get(job_id, {})
        if result.get("status") == "error":
            _JOBS[job_id] = {"status": "error", "ts": time.time(),
                             "reply": "I couldn't generate that — give me a bit more detail and try again."}
        else:
            _JOBS[job_id] = {"status": "done", "action": action, "asset_id": result.get("asset_id"),
                             "ts": time.time(),
                             "reply": f"Done — your {action.replace('_', ' ')} is ready. View it below."}
    except Exception:
        _JOBS[job_id] = {"status": "error", "ts": time.time(),
                         "reply": "Something went wrong generating that — please try again."}
    # opportunistic cleanup: drop jobs older than 30 min
    cutoff = time.time() - 1800
    for k in [k for k, v in _JOBS.items() if v.get("ts", 0) < cutoff]:
        _JOBS.pop(k, None)


def _chat(body: dict, client_ip: str) -> dict:
    msg = (body.get("message") or "").strip()
    if not msg:
        return {"status": "error", "error": "message required",
                "reply": "Tell me your startup name, what it does, and who it's for."}
    if not _rate_ok(client_ip):
        return {"status": "rate_limited",
                "reply": f"Easy there — max {CHAT_RATE_PER_MIN} messages/min. Give it a moment."}
    history = body.get("history") or []
    convo = "\n".join(f"{h.get('role', 'user')}: {h.get('content', '')}"
                      for h in history[-6:] if h.get("content"))
    raw, brain = _chat_brain(
        [{"role": "system", "content": _CHAT_SYSTEM},
         {"role": "user", "content": f"Conversation so far:\n{convo}\n\nLatest message: {msg}\n\nReturn routing JSON."}],
        max_tokens=900)
    routed = {}
    if raw:
        i, j = raw.find("{"), raw.rfind("}")
        if i != -1 and j > i:
            try:
                routed = json.loads(raw[i:j + 1])
            except Exception:
                routed = {}
    reply = (routed.get("reply") or "").strip()
    action = routed.get("action") or ""
    if routed.get("mode") == "action" and action in ACTIONS and action != "ask":
        call = {"action": action, "startup": routed.get("startup") or {},
                "session": _sid(body.get("session"))}
        params = routed.get("params") or {}
        for k in ("platform", "count", "role"):
            if params.get(k) not in (None, "", 0):
                call[k] = params[k]
        # Run the (slow) generation in the background so this request returns fast.
        job_id = uuid.uuid4().hex[:12]
        ack = reply or (f"On it — generating your {action.replace('_', ' ')}. "
                        "This takes ~1–2 min; I'll show it here when it's ready.")
        _JOBS[job_id] = {"status": "working", "action": action, "reply": ack, "ts": time.time()}
        threading.Thread(target=_run_action_job, args=(job_id, action, call), daemon=True).start()
        return {"status": "working", "mode": "working", "job_id": job_id, "brain": brain, "reply": ack}
    if not reply:
        ans, brain = _chat_brain(
            [{"role": "system", "content": "You are a concise, practical startup growth strategist."},
             {"role": "user", "content": msg}], max_tokens=600)
        reply = ans or "(brain unavailable — is the LLM up?)"
    return {"status": "done", "mode": "chat", "reply": reply, "brain": brain}


# --- routes ------------------------------------------------------------------
@app.post("/api/chat")
async def api_chat(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    ip = ((request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
          or (request.client.host if request.client else ""))
    return JSONResponse(_chat(body or {}, ip))


@app.get("/api/chat/job/{job_id}")
def api_chat_job(job_id: str) -> JSONResponse:
    j = _JOBS.get(re.sub(r"[^a-f0-9]", "", job_id))
    if not j:
        return JSONResponse({"status": "unknown"}, status_code=404)
    return JSONResponse(j)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "cores": {"erpnext": erp_connected(),
            "listmonk": listmonk_connected(), "postiz": postiz_reachable()}}


@app.get("/api/activity")
def activity(session: str = "") -> JSONResponse:
    return JSONResponse(fetch_activity(session=session))


@app.get("/api/assets")
def assets(session: str = "") -> JSONResponse:
    sid = _sid(session)
    items = [a for a in _load_assets() if not sid or _sid(a.get("session")) == sid]
    return JSONResponse({"assets": [{"id": a["id"], "type": a["type"], "title": a["title"],
                                     "startup": a.get("startup"), "created": a["created"]}
                                    for a in items]})


@app.get("/api/assets/{aid}")
def asset(aid: str, session: str = "") -> JSONResponse:
    f = ASSET_DIR / f"{re.sub(r'[^A-Za-z0-9-]', '', aid)}.json"
    if f.exists():
        doc = json.loads(f.read_text())
        # per-viewer isolation: an asset is only readable by its own session
        # (assets created without a session — e.g. internal /agent/run — stay public)
        owner = _sid(doc.get("session"))
        if owner and owner != _sid(session):
            return JSONResponse({"status": "error", "error": "not found"}, status_code=404)
        return JSONResponse(doc)
    return JSONResponse({"status": "error", "error": "not found"}, status_code=404)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return render(fetch_activity())


@app.post("/agent/run")
async def agent_run(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body or {}).get("action", "")
    if action in ACTIONS:
        return JSONResponse(ACTIONS[action](body or {}))
    return JSONResponse({"status": "error", "error": f"unknown action '{action}'",
                         "supported": list(ACTIONS)}, status_code=400)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive growth-assistant as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_growth_assistant_operator
    app.include_router(build_growth_assistant_operator().router())
except Exception:  # noqa: BLE001 — agentic_os absent → no operator surface
    pass
