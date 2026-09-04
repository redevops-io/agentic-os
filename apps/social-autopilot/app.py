"""agentic-social-autopilot — the social vertical slice on a real Postiz core.

Follows the agentic-billing reference pattern: an agent layer that reads REAL data
from the running self-hosted **Postiz** instance and renders an MD3 dashboard
(same design tokens as deploy/module_service.py) from that live data — no mock data.

How it reads Postiz (and why):
    Postiz exposes a NestJS REST API under /api (public API under /public/v1). In THIS
    stack that API never binds its HTTP port: the backend's onModuleInit tries to reach
    a Temporal server at 127.0.0.1:7233 that isn't deployed, so app.listen() never
    completes and nginx's /api -> :3000 proxy returns 502. The frontend (:4200) is up.

    So — exactly as the task allows — the agent reads the REAL scheduled posts, channels
    and follower counts straight from the Postiz **postgres** (container
    agentic-postiz-postiz-postgres-1, db/user/pass postiz), which seed.py populated. If a
    Temporal server is later added and the API comes up, `POSTIZ_API_URL` can be pointed
    at it and a REST `fetch_*` dropped in with no change to the dashboard.

Endpoints (mirror the billing reference):
    GET  /health        -> {"status","core":"postiz","connected": <bool>}
    GET  /api/activity  -> live KPIs + scheduled-post queue + per-network stats (from PG)
    GET  /              -> MD3 social dashboard rendered from the live data
    POST /agent/run     -> {"action":"draft"|"publish"}; publish is approval-gated

Config (env; seed.py writes agents/social-autopilot/.env automatically):
    POSTIZ_FRONT_URL    Postiz UI link for "Open in Postiz" (default http://localhost:4200)
    POSTIZ_PG_CONTAINER postgres container name (default agentic-postiz-postiz-postgres-1)
    POSTIZ_PG_USER/DB   postgres creds (default postiz/postiz)
    POSTIZ_ORG_ID       the seeded org id to scope reads to
    PORT                uvicorn port, default 8206
    OPENAI_API_KEY      OPTIONAL — preferred: /agent/run "draft" uses gpt-5.5 (SOCIAL_LLM_MODEL,
                        OPENAI_BASE_URL overridable) to write copy.
    ANTHROPIC_API_KEY   OPTIONAL — fallback after the self-hosted model; if set, draft uses Claude.
                        A deterministic template is always the final fallback.
"""
from __future__ import annotations

import html
import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

# --- config ------------------------------------------------------------------
# The Postiz postgres client, KPIs and agentic actions live in core.py (pure — no web /
# context-runtime deps) so the Mission Runtime operator can invoke them and they can be
# tested against a fake Postiz. Import config + core helpers from there; core loads .env.
from . import core  # noqa: E402
from .core import (  # noqa: E402
    TENANT, SUBTITLE, postiz_connected, fetch_activity,
)

PORT = int(os.environ.get("PORT", "8206"))

# --- product-demo reel: a UI agent records a live walkthrough (chat → planner → benchmarks) ---
# Headless Chrome won't run inside a container on this host (AppArmor/syscall wall), so the browser
# recorder runs as a HOST-side worker (reel/record_worker.mjs) that watches a spool. This container
# just drops a request into the spool and serves the resulting MP4 — reel/{out,spool} is bind-mounted.
_REEL_DIR = Path(__file__).resolve().parent / "reel"
_REEL_OUT = _REEL_DIR / "out" / "reel.mp4"
_REEL_STATUS = _REEL_DIR / "out" / "status.json"
_REEL_REQ = _REEL_DIR / "spool" / "record.request"

# vibexgen content-intelligence adapter (trends + AI generation; config-driven, demo-limited).
from . import vibex  # noqa: E402


def _reel_status_read() -> dict:
    try:
        d = json.loads(_REEL_STATUS.read_text())
    except Exception:
        d = {"status": "idle", "ts": 0, "duration": None, "error": None}
    return d


def _reel_start() -> dict:
    st = _reel_status_read()
    if st.get("status") == "recording":
        return {"status": "recording", "note": "already recording"}
    try:
        _REEL_REQ.parent.mkdir(parents=True, exist_ok=True)
        _REEL_STATUS.parent.mkdir(parents=True, exist_ok=True)
        _REEL_STATUS.write_text(json.dumps(
            {"status": "recording", "ts": time.time(), "duration": None, "error": None}))
        _REEL_REQ.write_text(str(time.time()))   # the host worker picks this up
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e)[:160]}
    return {"status": "recording",
            "note": "the UI agent is recording a ~45s walkthrough: chat → planner → benchmarks"}


app = FastAPI(title="agentic-social-autopilot (Meridian Wealth Management · core: Postiz)")


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
.feed{display:flex;flex-direction:column;gap:var(--sp-1)}
.feed__row{display:flex;align-items:flex-start;gap:var(--sp-4);padding:var(--sp-4) 0;border-bottom:1px solid var(--outline-variant)}
.feed__row:last-child{border-bottom:none}
.feed__net{flex:0 0 132px;display:flex;flex-direction:column;gap:var(--sp-1)}
.feed__when{color:var(--on-surface-muted);font:400 12px/16px var(--font-sans)}
.feed__what{flex:1;color:var(--on-surface);font:400 14px/20px var(--font-sans)}
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


def _tag_pill(tag: str) -> str:
    return {
        "scheduled": "pill--info", "draft": "pill--warn",
        "live": "pill--success", "error": "pill--danger",
    }.get(tag, "pill--neutral")


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
    """Surface the next post the agent could publish — always approval-gated."""
    nextup = next((p for p in data.get("queue", []) if p["state"] in ("QUEUE", "DRAFT")), None)
    if not nextup:
        return ""
    snippet = nextup["text"][:80] + ("…" if len(nextup["text"]) > 80 else "")
    return (
        "<div class='banner'>"
        "<span class='pill pill--warn'><span class='pill__dot'></span>publish · approval required</span>"
        "<span class='label-m' style='text-transform:uppercase;color:var(--warning)'>publish</span>"
        f"<span class='body-m'>Next up on {_esc(nextup['network'])} ({_esc(nextup['when'])}): "
        f"“{_esc(snippet)}” — the agent stages this; a human approves before it goes live.</span>"
        "</div>"
    )


def _queue_feed(data: dict) -> str:
    rows = ""
    for p in data["queue"]:
        tag = p["state_display"]
        text = p["text"][:140] + ("…" if len(p["text"]) > 140 else "")
        rows += (
            "<div class='feed__row'>"
            "<div class='feed__net'>"
            f"<span class='pill {_tag_pill(tag)}'><span class='pill__dot'></span>{_esc(tag)}</span>"
            f"<span class='feed__when'>{_esc(p['network'])} · {_esc(p['when'])}</span>"
            "</div>"
            f"<div class='feed__what'>{_esc(text)}</div>"
            "</div>"
        )
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Publishing queue · next 7 days</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from Postiz</span></div>"
        f"<div class='feed'>{rows}</div>"
        "</div>"
    )


def _network_table(data: dict) -> str:
    rows = ""
    for n in data["networks"]:
        rows += (
            "<tr>"
            f"<td>{_esc(n['network'])}</td>"
            f"<td class='num'>{_esc(n['followers_fmt'])}</td>"
            f"<td class='num'>{_esc(n['scheduled'])}</td>"
            f"<td class='num'>{_esc(n['engagement_pct'])}%</td>"
            "</tr>"
        )
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Per-network stats</h2></div>"
        "<table class='table'><thead><tr><th>Network</th><th>Followers</th><th>Scheduled</th><th>Eng. share</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "</div>"
    )


def _engagement_bars(data: dict) -> str:
    rows = ""
    for n in data["networks"]:
        pct = n["engagement_pct"]
        rows += (
            "<div class='barlist__row'>"
            f"<div class='barlist__label'>{_esc(n['network'])}</div>"
            f"<div class='bar'><span style='width:{pct}%'></span></div>"
            f"<div class='barlist__pct'>{pct}%</div>"
            "</div>"
        )
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Engagement by network (7d)</h2></div>"
        f"<div class='barlist'>{rows}</div>"
        "</div>"
    )


REEL_CSS = """
.reel-card{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-4)}
.reel-controls{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap}
.reel-status{color:var(--on-surface-muted);font-size:13px}
.reel-video{width:100%;max-width:880px;margin-top:var(--sp-4);border-radius:var(--radius-md);border:1px solid var(--outline-variant);background:#000;display:block}
.btn--ghost{background:transparent;border:1px solid var(--outline-variant);color:var(--on-surface-variant)}
"""

REEL_JS = """
(function(){
  var gen=document.getElementById('reel-gen'),st=document.getElementById('reel-status'),
      vid=document.getElementById('reel-video'),sch=document.getElementById('reel-sched');
  if(!gen) return;
  var timer=null;
  function showVideo(){ vid.src='api/reel/video?t='+Date.now(); vid.style.display='block'; sch.style.display='inline-flex'; }
  function poll(){
    fetch('api/reel/status').then(function(r){return r.json();}).then(function(d){
      if(d.status==='recording'){ st.textContent='Recording the walkthrough live… (~45s)'; gen.disabled=true; }
      else if(d.status==='done'){ if(timer)clearInterval(timer); gen.disabled=false; gen.textContent='▶ Re-record';
        st.textContent='Reel ready'+(d.duration?' · '+d.duration+'s':'')+' — review, then schedule.'; showVideo(); }
      else if(d.status==='error'){ if(timer)clearInterval(timer); gen.disabled=false; gen.textContent='▶ Generate demo reel';
        st.textContent='Recording failed: '+(d.error||'unknown'); }
      else if(d.ready){ st.textContent='A reel is ready — review, then schedule.'; showVideo(); }
    }).catch(function(){});
  }
  gen.addEventListener('click',function(){ st.textContent='Starting the UI agent…'; gen.disabled=true;
    fetch('api/reel/record',{method:'POST'}).then(function(){ if(timer)clearInterval(timer); timer=setInterval(poll,3000); poll(); }); });
  sch.addEventListener('click',function(){ sch.disabled=true; sch.textContent='Queuing…';
    fetch('api/reel/schedule',{method:'POST'}).then(function(r){return r.json();}).then(function(){
      sch.textContent='Queued as draft ✓'; st.textContent='Queued as a Postiz draft — attach the reel + pick channels in Postiz.';
    }).catch(function(){ sch.disabled=false; sch.textContent='Schedule as post ↗'; }); });
  poll();
})();
"""


def _reel_panel() -> str:
    return (
        "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Product demo reel &middot; UI agent</div>"
        "<div class='reel-card'>"
        "<div class='reel-controls'>"
        "<button id='reel-gen' class='btn' type='button'>▶ Generate demo reel</button>"
        "<span id='reel-status' class='reel-status'>A UI agent records a ~45s live walkthrough — "
        "chat.redevops.io → the planner → the benchmarks — as a ready-to-post reel.</span>"
        "<button id='reel-sched' class='btn btn--ghost' type='button' style='display:none'>Schedule as post ↗</button>"
        "</div>"
        "<video id='reel-video' class='reel-video' controls playsinline preload='none' style='display:none'></video>"
        "</div></section>"
    )


VIBEX_CSS = """
.vibex-input{flex:1;min-width:240px;background:var(--surface-container-high);border:1px solid var(--outline-variant);
  border-radius:var(--radius-pill);padding:9px 16px;color:var(--on-surface);font-size:14px;font-family:inherit;outline:none}
.vibex-input:focus{border-color:var(--primary)}
.vibex-mode{font:600 11px/1 ui-monospace,monospace;padding:3px 8px;border-radius:6px;margin-left:6px}
.vibex-mode.demo{background:var(--warning-container);color:var(--warning)}
.vibex-mode.live{background:var(--success-container);color:var(--success)}
.vibex-tags{display:flex;flex-wrap:wrap;gap:8px;margin-top:var(--sp-4)}
.vibex-tag{font:600 12.5px/1 ui-monospace,monospace;padding:6px 11px;border-radius:8px;background:var(--surface-container-high);
  border:1px solid var(--outline-variant);color:var(--primary)}
.vibex-angles{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;margin-top:var(--sp-4)}
.vibex-angle{background:var(--surface-container-high);border:1px solid var(--outline-variant);border-radius:var(--radius-md);padding:14px 16px}
.vibex-angle .fmt{font-weight:700;font-size:14px}
.vibex-angle .hook{color:var(--on-surface-variant);font-size:13px;margin:5px 0 4px}
.vibex-angle .why{color:var(--on-surface-muted);font-size:11.5px}
.vibex-angle .acts{display:flex;gap:8px;margin-top:10px}
.vibex-angle .mini{font:600 11.5px/1 inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--outline-variant);
  background:transparent;color:var(--on-surface-variant);cursor:pointer}
.vibex-angle .mini.primary{background:var(--primary);color:var(--on-primary);border-color:transparent}
"""

VIBEX_JS = """
(function(){
  var go=document.getElementById('vibex-go'),topic=document.getElementById('vibex-topic'),
      mode=document.getElementById('vibex-mode'),st=document.getElementById('vibex-status'),
      tags=document.getElementById('vibex-tags'),angles=document.getElementById('vibex-angles');
  if(!go) return;
  function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  fetch('api/vibex/status').then(function(r){return r.json();}).then(function(d){
    mode.textContent=d.mode==='live'?'vibexgen · live':'vibexgen · demo';
    mode.className='vibex-mode '+(d.mode==='live'?'live':'demo');
  }).catch(function(){});
  function render(d){
    tags.innerHTML=(d.hashtags||[]).map(function(h){return '<span class="vibex-tag">'+esc(h)+'</span>';}).join('');
    angles.innerHTML=(d.angles||[]).map(function(a){
      var hook=esc(a.hook||''),fmt=esc(a.format||''),seed=esc(a.seed||a.hook||''),
          gen=esc(a.seed||((a.format||'')+' — '+(a.hook||'')));
      return '<div class="vibex-angle"><div class="fmt">'+fmt+'</div><div class="hook">'+hook+
        '</div><div class="why">'+esc(a.why||'')+'</div><div class="acts">'+
        '<button class="mini primary" data-draft="'+seed+'">Draft post</button>'+
        '<button class="mini" data-gen="'+gen+'">Generate clip ▶</button></div></div>';
    }).join('');
    st.textContent=(d.source==='vibexgen'?'live from vibexgen · ':'')+ (d.angles||[]).length+' angles for “'+(d.topic||'')+'”';
    angles.querySelectorAll('[data-draft]').forEach(function(b){b.addEventListener('click',function(){
      b.disabled=true;b.textContent='Drafting…';
      fetch('api/vibex/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hook:b.dataset.draft})})
        .then(function(r){return r.json();}).then(function(){b.textContent='Drafted ✓';});});});
    angles.querySelectorAll('[data-gen]').forEach(function(b){b.addEventListener('click',function(){
      b.disabled=true;b.textContent='Requesting…';
      fetch('api/vibex/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:b.dataset.gen})})
        .then(function(r){return r.json();}).then(function(d){b.textContent=d.source==='vibexgen'?('Sent to vibexgen ✓'+(d.operation?' ('+esc(d.operation)+')':'')):'Queued (connect vibexgen)';});});});
  }
  function findTrends(){ st.textContent='Asking vibexgen…';
    fetch('api/vibex/trends',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({topic:topic.value})})
      .then(function(r){return r.json();}).then(render).catch(function(){st.textContent='vibexgen unreachable';}); }
  go.addEventListener('click',findTrends);
  topic.addEventListener('keydown',function(e){if(e.key==='Enter')findTrends();});
  findTrends();
})();
"""


def _vibex_panel() -> str:
    return (
        "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Content intelligence &middot; "
        "<span id='vibex-mode' class='vibex-mode demo'>vibexgen</span></div>"
        "<div class='reel-card'>"
        "<div class='reel-controls'>"
        "<input id='vibex-topic' class='vibex-input' placeholder='Topic — e.g. tax-loss harvesting, retirement income, market outlook'>"
        "<button id='vibex-go' class='btn' type='button'>Find trends</button>"
        "<span id='vibex-status' class='reel-status'></span>"
        "</div>"
        "<div id='vibex-tags' class='vibex-tags'></div>"
        "<div id='vibex-angles' class='vibex-angles'></div>"
        "</div></section>"
    )


def render(data: dict) -> str:
    connected = data["connected"]
    conn_txt = "core: Postiz connected" if connected else "core: Postiz UNREACHABLE"
    conn_cls = "pill--success" if connected else "pill--danger"
    status_pill = (
        f"<span class='pill {conn_cls}'><span class='pill__dot'></span>agent active · {_esc(conn_txt)}</span>"
    )
    live_badge = "<span class='pill pill--info'><span class='pill__dot'></span>data: live from Postiz</span>"
    open_btn = f"<a class='btn' href='{_esc(data['front_url'])}' target='_blank' rel='noopener'>Open in Postiz ↗</a>"

    body = (
        _approval_banner(data)
        + _kpi_tiles(data["kpis"])
        + _vibex_panel()
        + _reel_panel()
        + "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Content calendar</div>"
        "<div class='grid'>"
        f"<div class='col-8'>{_queue_feed(data)}</div>"
        f"<div class='col-4'>{_engagement_bars(data)}</div>"
        "</div>"
        "<div class='section-label'>Networks</div>"
        "<div class='grid'>"
        f"<div class='col-12'>{_network_table(data)}</div>"
        "</div></section>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentic Social Autopilot — {_esc(TENANT)}</title>
{FONT_LINK}
<style>{BASE_CSS}{PAGE_CSS}{REEL_CSS}{VIBEX_CSS}</style>
</head>
<body class="page">
<div class="shell">
  <header class="appbar">
    <div class="appbar__row">
      <h1>Social Autopilot</h1>
      {status_pill}
      {live_badge}
      <span class="spacer"></span>
      {open_btn}
    </div>
    <div class="appbar__tenant"><b>{_esc(TENANT)}</b> · core: Postiz (open-source social scheduling)</div>
    <div class="appbar__sub">{_esc(SUBTITLE)}</div>
  </header>
  {body}
  <footer class="footer">agentic-social-autopilot · live activity for {_esc(TENANT)} ·
    <a href="/api/activity">/api/activity</a> · agent + human, on a real Postiz core · redevops.io Agentic Business OS</footer>
</div>
<script>{REEL_JS}</script>
<script>{VIBEX_JS}</script>
</body>
</html>"""


# --- optional LLM copywriting (guarded: works without any API key) -----------
def _llm_copy(topic: str) -> str | None:
    """Draft post copy with Claude, or None if no key / any error. Optional by design —
    the template fallback always produces usable copy."""
    prompt = (
        "You write compliant social posts for a wealth-management firm (Meridian Wealth Management). "
        "Avoid performance promises, guarantees, or client testimonials. "
        f"Write ONE short, friendly social post about: {topic}. "
        "Include 1-2 relevant hashtags. Output only the post text, no preamble."
    )
    # Preferred: OpenAI gpt-5.5 (set OPENAI_API_KEY). Falls back to the self-hosted model,
    # then Claude, then the deterministic template — so the demo works with or without a key.
    okey = os.environ.get("OPENAI_API_KEY")
    if okey:
        try:
            r = httpx.post(
                os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {okey}"},
                json={"model": os.environ.get("SOCIAL_LLM_MODEL", "gpt-5.5"),
                      "messages": [{"role": "user", "content": prompt}],
                      "max_completion_tokens": 220},   # GPT-5 family uses max_completion_tokens
                timeout=30.0,
            )
            if r.status_code == 200:
                txt = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                if txt:
                    return txt
        except Exception:
            pass
    base = os.environ.get("REDEVOPS_LLM_BASE_URL")
    if base:
        try:
            r = httpx.post(
                base.rstrip("/") + "/chat/completions",
                json={"model": os.environ.get("REDEVOPS_LLM_MODEL", "DeepSeek-V4-Flash"),
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 220, "temperature": 0.3},
                timeout=90.0,   # DeepSeek runs on CPU (~15 tok/s) — be patient
            )
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
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                # claude-opus-4-8 is Anthropic's current Opus-tier model id.
                "model": "claude-opus-4-8",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": (
                    "You write compliant social posts for a wealth-management firm (Meridian Wealth Management). "
                    "Avoid performance promises, guarantees, or client testimonials. "
                    f"Write ONE short, friendly social post about: {topic}. "
                    "Include 1-2 relevant hashtags. Output only the post text, no preamble."
                )}],
            },
            timeout=15.0,
        )
        r.raise_for_status()
        return "".join(
            b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"
        ).strip() or None
    except Exception:
        return None


# --- agentic actions (thin wrappers over core; core stays context-runtime-free) ---
def _draft(body: dict) -> dict:
    """Draft post copy (optional LLM narration) and stage it as a DRAFT on the real Postiz core."""
    return core.draft(body, copy=_llm_copy)


def _publish(body: dict) -> dict:
    """Stage a publish for human approval (never auto-executed; approval-gated)."""
    return core.publish(body)


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "core": "postiz", "connected": postiz_connected()}


@app.get("/api/activity")
def activity() -> JSONResponse:
    return JSONResponse(fetch_activity())


# --- Context Runtime: live decisions over a synthetic goal stream ----------
import asyncio as _cr_asyncio
import json as _cr_json
from datetime import datetime as _cr_dt, timezone as _cr_tz
from fastapi.responses import StreamingResponse as _CRStreamingResponse

try:
    from context_runtime.integrations.social_autopilot import (  # type: ignore
        SocialAutopilotTenant as _CRTenant, social_bucket as _cr_bucket,
    )
    _CR = _CRTenant(epsilon=0.15)
except Exception:  # noqa: BLE001
    _CR = None

    def _cr_bucket(_t):  # type: ignore
        return "general"

_CR_SYNTH = [
    'Launch announcement',
    'Webinar reminder',
    'Community spotlight',
    'Promote the upgrade',
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


_CR_BANNER = """<div style="position:sticky;top:0;z-index:9998;background:linear-gradient(90deg,#10201d,#17171a);border-bottom:1px solid #2f2f33;color:#e4e2e6;font:13px/1.4 Roboto,system-ui,sans-serif;padding:9px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap"><span style="background:#4fd1c5;color:#08110f;font-weight:700;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">CONTEXT RUNTIME</span><span style="background:#2f2f33;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">DEMO</span><span style="color:#9b99a1">This demo app is plugged into <b style="color:#e4e2e6">Context Runtime</b>, which optimizes channel/timing/content strategy — engagement vs posting cost (3.88 vs 0.77). <a href="https://github.com/redevops-io/context-runtime" style="color:#4fd1c5;text-decoration:none">learn more \u2192</a></span></div>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    import re as _cr_re
    page = render(fetch_activity())
    page = _cr_re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + _CR_BANNER, page, count=1)
    if "_CR_BANNER" not in page and "cr-live" not in page:  # no <body> matched → prepend
        page = _CR_BANNER + page
    return (page.replace("</body>", _CR_LIVE_FEED + "</body>")
            if "</body>" in page else page + _CR_LIVE_FEED)


@app.post("/api/reel/record")
def api_reel_record() -> JSONResponse:
    """Start recording the product-demo reel (background; ~45s)."""
    return JSONResponse(_reel_start())


@app.get("/api/reel/status")
def api_reel_status() -> JSONResponse:
    return JSONResponse({**_reel_status_read(), "ready": _REEL_OUT.exists()})


@app.get("/api/reel/video")
def api_reel_video():
    if _REEL_OUT.exists():
        return FileResponse(str(_REEL_OUT), media_type="video/mp4",
                            filename="redevops-context-runtime-demo.mp4")
    return JSONResponse({"error": "no reel recorded yet"}, status_code=404)


@app.post("/api/reel/schedule")
def api_reel_schedule() -> JSONResponse:
    """Queue the reel as a Postiz draft (reuses the app's own draft path; nothing is published)."""
    draft = _draft({"topic": "our Context Runtime product demo — live retrieval, the planner "
                             "choosing a strategy, and measured v1→v2 benchmarks"})
    return JSONResponse({"status": "queued", "draft": draft,
                         "note": "queued as a Postiz DRAFT — attach the reel MP4 and pick channels in Postiz"})


# --- vibexgen: content intelligence (trends) + AI generation, exposed to the agent ----------
@app.get("/api/vibex/status")
def api_vibex_status() -> JSONResponse:
    return JSONResponse(vibex.status())


@app.post("/api/vibex/trends")
async def api_vibex_trends(request: Request) -> JSONResponse:
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return JSONResponse(vibex.trends((body or {}).get("topic", "")))


@app.post("/api/vibex/generate")
async def api_vibex_generate(request: Request) -> JSONResponse:
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return JSONResponse(vibex.generate((body or {}).get("prompt", ""), (body or {}).get("kind", "clip")))


@app.post("/api/vibex/draft")
async def api_vibex_draft(request: Request) -> JSONResponse:
    """Draft a post FROM a vibexgen trend angle — reuses the app's Postiz draft path."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    hook = (body or {}).get("hook") or (body or {}).get("topic") or "a trending wealth-management angle"
    return JSONResponse(_draft({"topic": hook}))


@app.post("/agent/run")
async def agent_run(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body or {}).get("action", "")

    if action == "draft":
        return JSONResponse(_draft(body or {}))
    if action == "publish":
        return JSONResponse(_publish(body or {}))
    if action == "record_reel":
        return JSONResponse(_reel_start())
    if action == "vibex_trends":
        return JSONResponse(vibex.trends((body or {}).get("topic", "")))
    if action == "vibex_generate":
        return JSONResponse(vibex.generate((body or {}).get("prompt", ""), (body or {}).get("kind", "clip")))
    return JSONResponse(
        {"status": "error", "error": f"unknown action '{action}'",
         "supported": ["draft", "publish", "record_reel", "vibex_trends", "vibex_generate"]},
        status_code=400,
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive social-autopilot as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_social_operator
    app.include_router(build_social_operator().router())
except Exception:  # noqa: BLE001 — agentic_os absent → no operator surface
    pass
