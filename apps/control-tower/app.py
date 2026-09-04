"""agentic-control-tower — the owner's single pane over a real Metabase core.

Copies the agentic-billing reference pattern (agents/billing/app.py) onto a different
OSS core: instead of Lago's REST collections, it wraps a running self-hosted
**Metabase** instance and runs REAL analytical queries via `POST /api/dataset`.

  * an agent layer that runs REAL Metabase queries (native SQL against the Sample DB)
    over the REST API, and
  * an MD3 dashboard (same design tokens as deploy/module_service.py, control-tower
    layout: "Ask anything" pill bar, KPI scorecards w/ sparklines, a revenue/trend bar
    chart, a breakdown table) rendered from those live query results — no mock data.

Pattern (same as billing):
  1. point CORE_API_URL / a session token at the running Metabase core,
  2. write `fetch_activity()` that runs real queries + computes KPIs,
  3. reuse BASE_CSS + the render helpers below,
  4. add agentic actions in /agent/run that are deterministic core API calls. Control
     Tower is read-only analytics — nothing moves money — so there is NO approval gate
     (noted in the /agent/run response), unlike billing's refund path.

Endpoints:
  GET  /health        -> {"status","core":"metabase","connected": <bool from GET /api/health>}
  GET  /api/activity  -> live KPI scorecards + a trend series + a breakdown, all from
                         live /api/dataset query results
  GET  /              -> MD3 control-tower dashboard rendered from the live data
  POST /agent/run     -> {"action":"ask","question":?} natural-language -> SQL template
                         -> /api/dataset -> answer + rows; {"action":"refresh"} re-runs
                         the dashboard queries.

Config (env; seed.py writes agents/control-tower/.env automatically):
  METABASE_API_URL    REST base, default http://localhost:3001
  METABASE_SESSION    X-Metabase-Session token (from /api/session or /api/setup)
  METABASE_DB_ID      database id to query (default 1 = Sample Database)
  METABASE_FRONT_URL  Metabase UI link for the "Open in Metabase" button
  PORT                uvicorn port, default 8202
  ANTHROPIC_API_KEY   OPTIONAL — if set, /agent/run "ask" adds an LLM reasoning blurb
                      AND can pick the best-matching question template; the endpoint
                      works fully without it (deterministic template routing).
"""
from __future__ import annotations

import html
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# --- config ------------------------------------------------------------------
# The Metabase REST client, KPIs and agentic analytics actions live in core.py (pure — no
# web / context-runtime / LLM deps) so the Mission Runtime operator can invoke them and they
# can be tested against a fake Metabase. Import config + core helpers from there; core loads
# agents/control-tower/.env. The LLM template-picker + narration blurb stay here (below).
from . import core
from .core import (  # noqa: E402
    METABASE_API_URL, METABASE_FRONT_URL, TENANT, SUBTITLE,
    metabase_connected, run_query, fetch_activity, QUESTION_TEMPLATES,
)

PORT = int(os.environ.get("PORT", "8202"))

app = FastAPI(title="agentic-control-tower (Meridian Wealth Management · core: Metabase)")


# --- MD3 styling (BASE_CSS reused verbatim from deploy/module_service.py) ------
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

# Per-module styling that builds on BASE_CSS — control-tower layout (Metabase/Looker
# style: ask pill, KPI scorecards w/ sparklines, revenue bars, breakdown table).
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
.barlist__row{display:grid;grid-template-columns:80px 1fr 96px;align-items:center;gap:var(--sp-4)}
.barlist__label{color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}
.barlist__pct{text-align:right;font-family:var(--font-mono);font-feature-settings:"tnum";font-size:13px;color:var(--on-surface-variant)}
.ask{display:flex;align-items:center;gap:var(--sp-3);background:var(--surface-container-highest);border:1px solid var(--outline-variant);border-radius:var(--radius-pill);padding:var(--sp-3) var(--sp-5)}
.ask__prompt{color:var(--primary);font-family:var(--font-mono);font-weight:500}
.ask__q{color:var(--on-surface);font:400 14px/20px var(--font-sans)}
.scorecard{display:flex;flex-direction:column;gap:var(--sp-2)}
.scorecard__foot{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3)}
.spark{display:block;width:100%;height:32px;margin-top:var(--sp-1)}
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


def _sparkline(values: list[float], color: str = "var(--primary)") -> str:
    """Inline SVG sparkline from a numeric series (no JS, no external libs)."""
    pts = [float(v) for v in (values or []) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    w, h, pad = 100.0, 28.0, 3.0
    n = len(pts)
    coords = []
    for i, v in enumerate(pts):
        x = pad + (w - 2 * pad) * (i / (n - 1))
        y = pad + (h - 2 * pad) * (1 - (v - lo) / span)
        coords.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(coords)
    last = coords[-1].split(",")
    return (
        f"<svg class='spark' viewBox='0 0 {w:.0f} {h:.0f}' preserveAspectRatio='none' aria-hidden='true'>"
        f"<polyline points='{poly}' fill='none' stroke='{color}' stroke-width='1.8' "
        "stroke-linecap='round' stroke-linejoin='round'/>"
        f"<circle cx='{last[0]}' cy='{last[1]}' r='1.8' fill='{color}'/>"
        "</svg>"
    )


def _kpi_scorecards(kpis: list[dict]) -> str:
    """KPI scorecards w/ sparklines (control-tower / Looker style)."""
    cells = ""
    for k in kpis:
        note = k.get("note", "")
        cls = "tile__delta"
        if note.startswith("↑") or note.startswith("+") or "↑" in note:
            cls += " tile__delta--up"
        elif note.startswith("↓") or note.startswith("-") or "↓" in note:
            cls += " tile__delta--down"
        spark = _sparkline(k.get("spark", []))
        cells += (
            "<div class='tile scorecard'>"
            f"<div class='tile__label'>{_esc(k['label'])}</div>"
            f"<div class='tile__value'>{_esc(k['value'])}</div>"
            f"<div class='scorecard__foot'><div class='{cls}'>{_esc(note)}</div></div>"
            f"{spark}"
            "</div>"
        )
    return f"<section class='kpi-row'>{cells}</section>"


def _ask_box() -> str:
    """A LIVE chatbox: type a question -> /api/ask -> a Metabase-driven chart renders below."""
    default_q = "Which service line makes the most margin?"
    chips = "".join(
        f"<button class='chip' type='button' data-q=\"{_esc(s)}\">{_esc(s)}</button>"
        for s in ASK_SUGGESTIONS
    )
    return (
        "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Ask anything &middot; live</div>"
        "<div class='askbox'><span class='ask__prompt'>&gt;_</span>"
        "<input id='ask-input' class='ask__input' autocomplete='off' "
        "placeholder='Ask your business anything — plain language, live against Metabase' "
        f"value=\"{_esc(default_q)}\">"
        "<button id='ask-btn' class='btn' type='button'>Ask</button></div>"
        f"<div class='ask-chips'>{chips}</div>"
        "<div id='ask-result' class='ask-result'>"
        "<div class='ask-hint'>Ask a question to run a live query against Metabase.</div></div>"
        "</section>"
    )


def _barlist(title: str, items: list[dict]) -> str:
    rows = ""
    for b in items:
        pct = int(b["pct"])
        rows += (
            "<div class='barlist__row'>"
            f"<div class='barlist__label'>{_esc(b['label'])}</div>"
            f"<div class='bar'><span style='width:{pct}%'></span></div>"
            f"<div class='barlist__pct'>{_esc(b.get('value', f'{pct}%'))}</div>"
            "</div>"
        )
    return (
        "<div class='card'>"
        f"<div class='card__head'><h2 class='card__title'>{_esc(title)}</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from Metabase</span></div>"
        f"<div class='barlist'>{rows}</div>"
        "</div>"
    )


def _table_card(table: dict) -> str:
    head = "".join(f"<th>{_esc(h)}</th>" for h in table["head"])
    body = ""
    for row in table["rows"]:
        cells = ""
        for i, c in enumerate(row):
            txt = str(c)
            if i > 0:
                cells += f"<td class='num'>{_esc(txt)}</td>"
            else:
                cells += f"<td>{_esc(txt)}</td>"
        body += f"<tr>{cells}</tr>"
    return (
        "<div class='card'>"
        f"<div class='card__head'><h2 class='card__title'>{_esc(table['title'])}</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from Metabase</span></div>"
        f"<table class='table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        "</div>"
    )


def _section(label: str, body: str) -> str:
    return (
        "<section class='shell' style='gap:var(--sp-4)'>"
        f"<div class='section-label'>{_esc(label)}</div>{body}</section>"
    )


ASK_CSS = """
.askbox{display:flex;align-items:center;gap:var(--sp-3);background:var(--surface-container);
  border:1px solid var(--outline-variant);border-radius:var(--radius-pill);padding:6px 6px 6px var(--sp-4)}
.askbox:focus-within{border-color:var(--primary)}
.ask__input{flex:1;background:transparent;border:0;outline:0;color:var(--on-surface);font-size:15px;font-family:inherit}
.ask__input::placeholder{color:var(--on-surface-muted)}
.ask-chips{display:flex;flex-wrap:wrap;gap:var(--sp-2)}
.chip{background:var(--surface-container-high);color:var(--on-surface-variant);border:1px solid var(--outline-variant);
  border-radius:var(--radius-pill);padding:6px 12px;font-size:12.5px;cursor:pointer;font-family:inherit}
.chip:hover{border-color:var(--primary);color:var(--primary)}
.ask-result{min-height:44px}
.ask-hint{color:var(--on-surface-muted);font-size:13px}
.ask-answer{color:var(--on-surface);font-size:15px;font-weight:600;margin-bottom:var(--sp-1)}
.ask-reason{color:var(--on-surface-muted);font-size:12.5px;margin-bottom:var(--sp-3)}
.ask-loading{color:var(--primary);font-size:13px}
"""

ASK_JS = """
(function(){
  var input=document.getElementById('ask-input'),btn=document.getElementById('ask-btn'),out=document.getElementById('ask-result');
  if(!input) return;
  function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function fmt(v,f){v=Number(v)||0;if(f==='money')return '$'+Math.round(v).toLocaleString();if(f==='pct')return Math.round(v)+'%';return Math.round(v).toLocaleString();}
  function card(title,inner){return '<div class="card"><div class="card__head"><h2 class="card__title">'+esc(title)+
    '</h2><span class="pill pill--info"><span class="pill__dot"></span>live from Metabase</span></div>'+inner+'</div>';}
  function render(d){
    if(!d.rows||!d.rows.length){out.innerHTML='<div class="ask-hint">'+esc(d.answer||'No data for that question.')+'</div>';return;}
    var html='<div class="ask-answer">'+esc(d.answer||'')+'</div>';
    if(d.reasoning) html+='<div class="ask-reason">'+esc(d.reasoning)+'</div>';
    if(d.chart==='bar'||d.chart==='line'){
      var xi=d.x_i,yi=d.y_i,f=d.yfmt,max=1;
      d.rows.forEach(function(r){max=Math.max(max,Math.abs(Number(r[yi])||0));});
      var bars=d.rows.map(function(r){var v=Number(r[yi])||0,pct=Math.round(100*Math.abs(v)/max);
        return '<div class="barlist__row"><div class="barlist__label">'+esc(r[xi])+'</div><div class="bar"><span style="width:'+pct+'%"></span></div><div class="barlist__pct">'+fmt(v,f)+'</div></div>';}).join('');
      html+=card(d.title,'<div class="barlist">'+bars+'</div>');
    }else{
      var th=(d.columns||[]).map(function(c){return '<th>'+esc(c)+'</th>';}).join('');
      var tb=d.rows.map(function(r){return '<tr>'+r.map(function(c,i){return '<td'+(i>0?' class="num"':'')+'>'+esc(c)+'</td>';}).join('')+'</tr>';}).join('');
      html+=card(d.title,'<table class="table"><thead><tr>'+th+'</tr></thead><tbody>'+tb+'</tbody></table>');
    }
    out.innerHTML=html;
  }
  function ask(q){q=(q||'').trim();if(!q)return;out.innerHTML='<div class="ask-loading">Running a live query against Metabase…</div>';
    fetch('api/ask?q='+encodeURIComponent(q)).then(function(r){return r.json();}).then(render).catch(function(){out.innerHTML='<div class="ask-hint">Could not reach the analyst agent.</div>';});}
  btn.addEventListener('click',function(){ask(input.value);});
  input.addEventListener('keydown',function(e){if(e.key==='Enter')ask(input.value);});
  Array.prototype.forEach.call(document.querySelectorAll('.chip'),function(c){c.addEventListener('click',function(){input.value=c.dataset.q;ask(c.dataset.q);});});
  ask(input.value);
})();
"""


def render(data: dict) -> str:
    connected = data["connected"]
    conn_txt = "core: Metabase connected" if connected else "core: Metabase UNREACHABLE"
    conn_cls = "pill--success" if connected else "pill--danger"
    status_pill = (
        f"<span class='pill {conn_cls}'><span class='pill__dot'></span>agent active · {_esc(conn_txt)}</span>"
    )
    live_badge = ("<span class='pill pill--info'><span class='pill__dot'></span>"
                  "data: live from Metabase · demo data</span>")
    open_btn = (
        f"<a class='btn' href='{_esc(data['front_url'])}' target='_blank' rel='noopener' "
        "title='Opens the read-only Metabase dashboard (demo data) in a new tab'>"
        "Open in Metabase · demo data ↗</a>"
    )

    body = (
        _kpi_scorecards(data["kpis"])
        + _ask_box()
        + _section("Revenue trend", _barlist(data["bars"]["title"], data["bars"]["items"]))
        + _section("Breakdown", _table_card(data["table"]))
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Control Tower — {_esc(TENANT)}</title>
{FONT_LINK}
<style>{BASE_CSS}{PAGE_CSS}{ASK_CSS}</style>
</head>
<body class="page">
<div class="shell">
  <header class="appbar">
    <div class="appbar__row">
      <h1>Control Tower</h1>
      {status_pill}
      {live_badge}
      <span class="spacer"></span>
      {open_btn}
    </div>
    <div class="appbar__tenant"><b>{_esc(TENANT)}</b> · core: Metabase (open-source BI), as of {_esc(data['as_of'])}</div>
    <div class="appbar__sub">{_esc(SUBTITLE)}</div>
  </header>
  {body}
  <footer class="footer">agentic-control-tower · live activity for {_esc(TENANT)} ·
    <a href="/api/activity">/api/activity</a> · agent + human, on a real Metabase core · redevops.io Agentic Business OS</footer>
</div>
<script>{ASK_JS}</script>
</body>
</html>"""


# --- optional LLM assist (guarded: works without any API key) -----------------
def _llm_pick_template(question: str) -> str | None:
    """Use Claude to pick a template key, or None if no key / any error.

    Optional by design — keyword routing below always works. The LLM only chooses
    among PRE-WRITTEN SQL templates; it never authors SQL.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not question:
        return None
    keys = ", ".join(t["key"] for t in QUESTION_TEMPLATES)
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                # claude-opus-4-8 is Anthropic's current Opus-tier model id.
                "model": "claude-opus-4-8",
                "max_tokens": 20,
                "messages": [{
                    "role": "user",
                    "content": f"Pick the single best report key for this business question. "
                               f"Reply with ONLY the key, nothing else.\nKeys: {keys}\nQuestion: {question}",
                }],
            },
            timeout=12.0,
        )
        r.raise_for_status()
        txt = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
        return txt if txt in {t["key"] for t in QUESTION_TEMPLATES} else None
    except Exception:
        return None


def _llm_blurb(prompt: str) -> str | None:
    """Return a one-line reasoning blurb from Claude, or None if no key / any error."""
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
                "model": "claude-opus-4-8",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15.0,
        )
        r.raise_for_status()
        return "".join(
            b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"
        ).strip() or None
    except Exception:
        return None


# --- agentic actions (thin wrappers over core; core stays context-runtime/LLM-free) ---
def _ask(question: str) -> dict:
    """NL question -> live Metabase query, with the optional LLM template-picker + blurb.

    The pure routing/query/viz logic lives in core.ask; here we inject the two LLM helpers
    (`_llm_pick_template`, `_llm_blurb`) that call out to a model. Read-only analytics: no
    approval gate, and only ever the pre-written template SQL runs — never model-authored SQL.
    """
    return core.ask(question, pick=_llm_pick_template, blurb=_llm_blurb)


def _refresh() -> dict:
    """Re-run the dashboard queries (bust the cache) and report fresh KPIs."""
    return core.refresh()


# --- routes -------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "core": "metabase", "connected": metabase_connected()}


@app.get("/api/activity")
def activity() -> JSONResponse:
    return JSONResponse(fetch_activity())


@app.get("/api/ask")
def api_ask(q: str = "") -> JSONResponse:
    """Live chatbox: a natural-language question -> routed template -> REAL Metabase query ->
    a viz-ready result (chart hint + rows). Under /api/ so the demo control-plane proxies it;
    read-only (only pre-written template SQL runs)."""
    q = (q or "").strip()
    if not q:
        return JSONResponse({"status": "empty", "answer": "Ask a question about the business.",
                             "rows": [], "columns": [], "chart": "table"})
    return JSONResponse(_ask(q))


# a few discoverable starter questions for the chatbox (title -> example prompt)
ASK_SUGGESTIONS = [
    "Which service line makes the most margin?",
    "Where are my best client referrals coming from?",
    "Advisory-fee revenue by month",
    "Which fees are outstanding?",
    "What's my proposal-to-client conversion?",
    "Revenue by region",
]


# --- Context Runtime: live decisions over a synthetic question stream ----------
import asyncio as _cr_asyncio
import json as _cr_json
from datetime import datetime as _cr_dt, timezone as _cr_tz
from fastapi.responses import StreamingResponse as _CRStreamingResponse

try:
    from context_runtime.integrations.control_tower import (  # type: ignore
        ControlTowerTenant as _CRTenant, control_tower_bucket as _cr_bucket,
    )
    _CR = _CRTenant(epsilon=0.15)
except Exception:  # noqa: BLE001
    _CR = None

    def _cr_bucket(_t):  # type: ignore
        return "general"

_CR_SYNTH = [
    "What's AUM this month?",
    'Net flows by segment',
    'Accounts needing review',
    'Advisory-fee revenue this quarter',
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


_CR_BANNER = """<div style="position:sticky;top:0;z-index:9998;background:linear-gradient(90deg,#10201d,#17171a);border-bottom:1px solid #2f2f33;color:#e4e2e6;font:13px/1.4 Roboto,system-ui,sans-serif;padding:9px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap"><span style="background:#4fd1c5;color:#08110f;font-weight:700;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">CONTEXT RUNTIME</span><span style="background:#2f2f33;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">DEMO</span><span style="color:#9b99a1">This demo app is plugged into <b style="color:#e4e2e6">Context Runtime</b>, which optimizes which queries to run — answer correctness vs query cost (5.33 vs 1.64). <a href="https://github.com/redevops-io/context-runtime" style="color:#4fd1c5;text-decoration:none">learn more \u2192</a></span></div>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    import re as _cr_re
    page = render(fetch_activity())
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

    if action == "ask":
        return JSONResponse(_ask((body or {}).get("question", "")))
    if action == "refresh":
        return JSONResponse(_refresh())
    return JSONResponse(
        {"status": "error", "error": f"unknown action '{action}'",
         "supported": ["ask", "refresh"]},
        status_code=400,
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive Control Tower as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_control_tower_operator
    app.include_router(build_control_tower_operator().router())
except Exception:  # noqa: BLE001 — agentic_os absent → no operator surface
    pass
