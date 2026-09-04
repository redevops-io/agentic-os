"""agentic-market-radar — agentic module on a real changedetection.io core.

Built on the agentic-billing reference pattern: wrap a running self-hosted OSS core
(here **changedetection.io**, the open-source website-change-monitoring engine) with

  * an agent layer that reads REAL watch data over the REST API, and
  * an MD3 dashboard (same design tokens as deploy/module_service.py, market-radar
    layout) rendered from that live data — no mock data.

Competitive-intelligence (Crayon/Klue) for a wealth-management firm: every watch is a competitor
RIA or fee/regulatory page; the agent surfaces what changed and can add new monitors.

Endpoints:
  GET  /health        -> {"status","core":"changedetection","connected": <bool>}
  GET  /api/activity  -> live KPIs + watch list derived from changedetection REST
  GET  /              -> MD3 market-radar dashboard rendered from the live watches
  POST /agent/run     -> agentic action: {"action":"add_watch"|"brief"}

Config (env; seed.py writes agents/market-radar/.env automatically):
  CD_API_URL    REST base, default http://localhost:5001
  CD_API_KEY    changedetection api token (settings.application.api_access_token);
                sent as the `x-api-key` header
  CD_FRONT_URL  changedetection UI link for the "Open in changedetection" button
  PORT          uvicorn port, default 8204
  ANTHROPIC_API_KEY  OPTIONAL — if set, the "brief" action adds an LLM summary;
                     the endpoint works fully without it.
"""
from __future__ import annotations

import html
import os

# ── enterprise permissions plane — gate tool/data access by (app, user) ──
# No-op without the context_runtime_enterprise package or CR_PERMISSIONS=1 (open-core default).
try:
    from context_runtime_enterprise.apps import bootstrap as _cr_bootstrap
    _req_principal = _cr_bootstrap("market-radar")
except Exception:  # noqa: BLE001
    def _req_principal(request=None):
        return None
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
try:
    from context_runtime.adapters.model_openai import OpenAICompatibleModel
    from context_runtime.types import ModelRequest
except Exception:  # pragma: no cover - context-runtime optional
    OpenAICompatibleModel = None
    ModelRequest = None
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

try:
    _BLURB_MODEL = OpenAICompatibleModel.from_env() if OpenAICompatibleModel else None
except Exception:
    _BLURB_MODEL = None

# --- config ------------------------------------------------------------------
# The changedetection REST client, KPIs and agentic actions live in core.py (pure — no
# web / context-runtime deps) so the Mission Runtime operator can invoke them and they can
# be tested against a fake changedetection. Import config + core helpers from there; core
# loads .env.
from . import core
from .core import (  # noqa: E402
    CD_API_URL, CD_API_KEY, CD_FRONT_URL, TENANT, SUBTITLE,
    _headers, cd_systeminfo, cd_connected, _get, fetch_activity,
)

PORT = int(os.environ.get("PORT", "8204"))

app = FastAPI(title="agentic-market-radar (Meridian Wealth Management · core: changedetection)")


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

# market-radar specific tokens (compgrid/comp + feed), reused from module_service.py
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
.compgrid{display:grid;gap:var(--sp-4);grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.comp{background:var(--surface-container-high);border:1px solid var(--outline-variant);border-radius:var(--radius-md);padding:var(--sp-4);display:flex;flex-direction:column;gap:var(--sp-2)}
.comp__name{font:500 16px/24px var(--font-sans);color:var(--on-surface)}
.comp__pos{color:var(--on-surface-muted);font:400 13px/18px var(--font-sans);font-family:var(--font-mono)}
.comp__meta{display:flex;align-items:center;gap:var(--sp-2);flex-wrap:wrap;margin-top:var(--sp-1)}
.feed{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.feed__item{display:grid;grid-template-columns:160px 1fr auto auto;gap:var(--sp-4);align-items:center;padding:var(--sp-3) 0;border-bottom:1px solid var(--outline-variant)}
.feed__item:last-child{border-bottom:none}
.feed__who{color:var(--primary);font:500 12px/16px var(--font-sans)}
.feed__what{color:var(--on-surface);font:400 14px/20px var(--font-sans)}
.feed__when{color:var(--on-surface-muted);font:400 12px/16px var(--font-sans);white-space:nowrap;font-family:var(--font-mono)}
@media(max-width:839px){.feed__item{grid-template-columns:1fr auto}}
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


def _state_pill(state: str) -> tuple[str, str]:
    """Return (pill-class, label) for a watch state."""
    return {
        "changed": ("pill--warn", "change · unread"),
        "error": ("pill--neutral", "source unreachable"),
        "stable": ("pill--success", "no change"),
        "pending": ("pill--neutral", "pending check"),
    }.get(state, ("pill--neutral", state))


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


def _alert_banner(data: dict) -> str:
    """Show when watches have unread changes (the agent can brief on them)."""
    unread = [w for w in data["watches"] if w["unread"]]
    if not unread:
        return ""
    first = unread[0]
    extra = f" (+{len(unread) - 1} more)" if len(unread) > 1 else ""
    return (
        "<div class='banner'>"
        f"<span class='pill pill--warn'><span class='pill__dot'></span>{len(unread)} unread change(s)</span>"
        "<span class='label-m' style='text-transform:uppercase;color:var(--warning)'>brief</span>"
        f"<span class='body-m'>{_esc(first['title'])} changed {_esc(first['last_changed_h'])}{_esc(extra)}. "
        "Agent can brief you on what moved (read-only — no approval needed).</span>"
        "</div>"
    )


def _competitor_grid(data: dict) -> str:
    """Competitor 'watch' card grid with last-change pills (Crayon/Klue style)."""
    cells = ""
    for w in data["watches"]:
        pill_cls, label = _state_pill(w["state"])
        tags = "".join(
            f"<span class='pill pill--neutral'>{_esc(t)}</span>" for t in w["tags"]
        ) or "<span class='pill pill--neutral'>untagged</span>"
        cells += (
            "<div class='comp'>"
            "<div class='card__head'>"
            f"<span class='comp__name'>{_esc(w['title'])}</span>"
            f"<span class='pill {pill_cls}'>{_esc(label)}</span>"
            "</div>"
            f"<div class='comp__pos'>{_esc(w['short_url'])}</div>"
            f"<div class='comp__meta'>{tags}</div>"
            f"<div class='comp__meta'><span class='body-s' style='color:var(--on-surface-muted)'>"
            f"checked {_esc(w['last_checked_h'])} · changed {_esc(w['last_changed_h'])}</span></div>"
            "</div>"
        )
    if not cells:
        cells = ("<div class='comp'><div class='comp__pos'>No watches yet — seed competitor "
                 "pages or POST /agent/run {\"action\":\"add_watch\"}.</div></div>")
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Competitor watches</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from changedetection</span></div>"
        f"<div class='compgrid'>{cells}</div>"
        "</div>"
    )


def _change_feed(data: dict) -> str:
    """Change-feed table: every watch + its last-checked / last-change state."""
    rows = ""
    for w in data["watches"]:
        pill_cls, label = _state_pill(w["state"])
        rows += (
            "<tr>"
            f"<td>{_esc(w['title'])}</td>"
            f"<td>{_esc(w['tag_str'])}</td>"
            f"<td>{_esc(w['last_checked_h'])}</td>"
            f"<td>{_esc(w['last_changed_h'])}</td>"
            f"<td><span class='pill {pill_cls}'>{_esc(label)}</span></td>"
            "</tr>"
        )
    if not rows:
        rows = "<tr><td colspan='5'>No watches yet.</td></tr>"
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Change feed</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from changedetection</span></div>"
        "<table class='table'><thead><tr>"
        "<th>Watch</th><th>Tags</th><th>Last checked</th><th>Last change</th><th>State</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "</div>"
    )


def render(data: dict) -> str:
    connected = data["connected"]
    conn_txt = "core: changedetection connected" if connected else "core: changedetection UNREACHABLE"
    conn_cls = "pill--success" if connected else "pill--danger"
    status_pill = (
        f"<span class='pill {conn_cls}'><span class='pill__dot'></span>agent active · {_esc(conn_txt)}</span>"
    )
    live_badge = "<span class='pill pill--info'><span class='pill__dot'></span>data: live from changedetection</span>"
    open_btn = (f"<a class='btn' href='{_esc(data['front_url'])}' target='_blank' rel='noopener'>"
                "Open in changedetection ↗</a>")

    body = (
        _alert_banner(data)
        + _kpi_tiles(data["kpis"])
        + "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Positioning</div>"
        f"{_competitor_grid(data)}"
        "</section>"
        "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Change feed</div>"
        f"{_change_feed(data)}"
        "</section>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Radar — {_esc(TENANT)}</title>
{FONT_LINK}
<style>{BASE_CSS}{PAGE_CSS}</style>
</head>
<body class="page">
<div class="shell">
  <header class="appbar">
    <div class="appbar__row">
      <h1>Market Radar</h1>
      {status_pill}
      {live_badge}
      <span class="spacer"></span>
      {open_btn}
    </div>
    <div class="appbar__tenant"><b>{_esc(TENANT)}</b> · core: changedetection.io (open-source change monitoring)</div>
    <div class="appbar__sub">{_esc(SUBTITLE)}</div>
  </header>
  {body}
  <footer class="footer">agentic-market-radar · live activity for {_esc(TENANT)} ·
    <a href="/api/activity">/api/activity</a> · agent + human, on a real changedetection.io core · redevops.io Agentic Business OS</footer>
</div>
</body>
</html>"""


# --- optional LLM reasoning blurb (guarded: works without any API key) -------
def _llm_blurb(prompt: str) -> str | None:
    """Return a short summary from Claude, or None if no key / any error.

    Optional by design — the "brief" action is fully functional from the real watch
    data alone; the LLM only adds prose. Absence of ANTHROPIC_API_KEY must never
    break the endpoint.
    """
    if _BLURB_MODEL is None:
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
        return None
    try:
        res = _BLURB_MODEL.complete(
            ModelRequest(
                messages=({"role": "user", "content": prompt},),
                max_tokens=220,
            )
        )
        return res.text
    except Exception:
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
        return None


# --- agentic actions (thin wrappers over core; core stays context-runtime-free) ---
def _add_watch(body: dict) -> dict:
    """Start monitoring a new page on the real changedetection core (non-destructive)."""
    return core.add_watch(body)


def _brief(body: dict) -> dict:
    """Competitive-intelligence brief over the real core, with the optional LLM narration."""
    return core.brief(body, blurb=_llm_blurb)


# ──────── conversational agent · Context Runtime (LLM) + redevops-rag (grounding) + agent-harness (tools) ────────
try:
    from context_runtime.integrations.agent_console import AgentConsole as _AgentConsole, tool as _crtool
except Exception:  # noqa: BLE001 — context-runtime absent → no console
    _AgentConsole = None

_RADAR_PRIMER = """A WATCH is a single web page changedetection.io monitors for you. Add one under Watches → Add a new watch (paste the URL), or ask the agent to "watch example.com". Each watch is a competitor, custodian or fee/regulatory page you want to be told about the moment it moves.

CHECK FREQUENCY is how often a watch is re-fetched — set per watch (e.g. every 3 hours, daily, weekly). Faster catches changes sooner but scrapes more; slower is cheaper. Tune it on the watch's Edit → "Time between check" so busy competitor pages are checked often and stable ones rarely.

A FILTER narrows a watch to only the part of a page you care about, so unrelated edits (nav, ads, timestamps) don't trigger a false alert. Use a CSS selector (e.g. .price, #stock-status), an XPath, or a JSON key for API pages. Set it on the watch's Edit → Filters & Triggers → "CSS/JSONPath/XPath filter". This is how you "track just the price" — filter to the price element and everything else is ignored.

A DIFF is the before/after comparison changedetection stores every time the filtered content changes. The change history keeps every past version; open a watch to see exactly what text was added or removed. The dashboard's "unread" count is watches whose latest diff nobody has looked at yet.

A NOTIFICATION is how you get told a watch changed — email, Slack, Discord, Telegram or a webhook, configured under Settings → Notifications or per watch. Without one you only see changes on the dashboard; add one so a competitor price cut reaches you within a check cycle.

PRICE / STOCK TRACKING is the classic use: watch a product or pricing page, add a CSS/JSON filter to the price or "in stock" element, and set a fast check frequency. changedetection then alerts you only when the number or availability actually changes — ignore layout churn entirely.

WHY A CHANGE WASN'T DETECTED, common causes: the page renders its content with JavaScript (needs the browser/Playwright fetcher, not plain HTTP); a filter is too narrow and the change happened outside it; the check frequency hasn't elapsed yet; or the site blocks scrapers. Widen or remove the filter, switch the watch to the browser fetcher, or check more often.

Every watch here is competitive intelligence for a wealth-management firm (Meridian Wealth Management): competitor RIA fee schedules, custodian and market-data feeds, and regulatory/SEC pages. The agent can summarize what changed, list what's being watched, and add a new watch on request."""


def _t_watch_summary(_a: dict) -> dict:
    d = fetch_activity()
    c = d["counts"]
    conn = "connected" if d["connected"] else "UNREACHABLE"
    return {"text": f"{c['watches']} watch(es) tracked · {c['changes_week']} changed this week · "
            f"{c['price_moves']} price move(s) · {c['unread']} unread. Core: changedetection ({conn}).",
            "data": d["kpis"]}


def _t_recent_changes(_a: dict) -> dict:
    d = fetch_activity()
    changed = [w for w in d["watches"] if w["last_changed"]]
    if not changed:
        return {"text": "No changes recorded yet across the tracked pages — all watches are stable "
                "or awaiting their first diff.", "data": []}
    changed.sort(key=lambda w: w["last_changed"], reverse=True)
    lines = [f"{w['title']} ({w['short_url']}) — changed {w['last_changed_h']}"
             + (" · unread" if w["unread"] else "") for w in changed[:10]]
    return {"text": f"{len(changed)} watch(es) with recorded changes:\n" + "\n".join(lines), "data": changed}


def _t_list_watches(_a: dict) -> dict:
    d = fetch_activity()
    ws = d["watches"]
    if not ws:
        return {"text": "No watches yet — add one with \"watch <url>\" or in changedetection.", "data": []}
    lines = [f"{w['title']} · {w['short_url']} · [{w['tag_str']}] · {w['state']} "
             f"(checked {w['last_checked_h']})" for w in ws[:20]]
    return {"text": f"{len(ws)} monitored page(s):\n" + "\n".join(lines), "data": ws}


def _t_add_watch(a: dict) -> dict:
    r = _add_watch(a or {})
    return {"text": r.get("summary") or r.get("error") or f"add_watch · {r.get('status', 'done')}.", "data": r}


if _AgentConsole is not None:
    _RADAR_CONSOLE = _AgentConsole(
        f"{TENANT} Market Radar", _RADAR_PRIMER,
        tools=[
            _crtool("watch_summary", "how many watches, how many changed this week, price moves and unread", _t_watch_summary),
            _crtool("recent_changes", "what changed lately across the monitored competitor/price pages", _t_recent_changes),
            _crtool("list_watches", "list the monitored URLs, their tags and current state", _t_list_watches),
            _crtool("add_watch", "start monitoring a new page (non-destructive)", _t_add_watch, side_effecting=True,
                    parameters={"type": "object", "properties": {"url": {"type": "string"}, "title": {"type": "string"}}}),
        ],
        suggestions=["What changed this week?", "Search the web for changedetection alternatives",
                     "How do I track just the price?", "What is a filter?"],
        subtitle="Ask what moved, search the web for competitor signals, or add a new monitor.",
        allow_side_effects=["add_watch"],  # adding a watch is safe/reversible → runs from chat
    )
else:
    _RADAR_CONSOLE = None

# Mount a REAL MCP tool: the bundled keyless web-search server (Wikipedia + Hacker News) so the
# assistant can look up competitors / market signals beyond the watched pages. It's read-only
# (readOnlyHint) so the agent-harness ApprovalPolicy lets it run ungated. Optional — the app
# runs fine if the MCP server can't start.
if _RADAR_CONSOLE is not None:
    try:
        import sys as _sys
        from context_runtime.tools.mcp import MCPClient as _MCPClient
        _RADAR_MCP = _MCPClient.stdio([_sys.executable, "-m", "context_runtime.tools.mcp_servers.web_search"])
        _RADAR_CONSOLE.mount_mcp(_RADAR_MCP)
    except Exception:  # noqa: BLE001
        _RADAR_MCP = None


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "core": "changedetection", "connected": cd_connected()}


@app.post("/api/agent")
async def api_agent(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if _RADAR_CONSOLE is None:
        return JSONResponse({"intent": "help", "text": "The assistant is offline (context-runtime unavailable).", "evidence": []})
    return JSONResponse(_RADAR_CONSOLE.respond((body or {}).get("message", ""), principal=_req_principal(request)))


@app.get("/api/activity")
def activity() -> JSONResponse:
    return JSONResponse(fetch_activity())


# --- Context Runtime: live decisions over a synthetic question stream ----------
import asyncio as _cr_asyncio
import json as _cr_json
from datetime import datetime as _cr_dt, timezone as _cr_tz
from fastapi.responses import StreamingResponse as _CRStreamingResponse

try:
    from context_runtime.integrations.market_radar import (  # type: ignore
        MarketRadarTenant as _CRTenant, market_radar_bucket as _cr_bucket,
    )
    _CR = _CRTenant(epsilon=0.15)
except Exception:  # noqa: BLE001
    _CR = None

    def _cr_bucket(_t):  # type: ignore
        return "general"

_CR_SYNTH = [
    'Did a competitor change pricing?',
    'Any new product release?',
    'Is a competitor hiring aggressively?',
    'Breaking market news?',
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


_CR_BANNER = """<div style="position:sticky;top:0;z-index:9998;background:linear-gradient(90deg,#10201d,#17171a);border-bottom:1px solid #2f2f33;color:#e4e2e6;font:13px/1.4 Roboto,system-ui,sans-serif;padding:9px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap"><span style="background:#4fd1c5;color:#08110f;font-weight:700;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">CONTEXT RUNTIME</span><span style="background:#2f2f33;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">DEMO</span><span style="color:#9b99a1">This demo app is plugged into <b style="color:#e4e2e6">Context Runtime</b>, which optimizes which competitor watches to sweep — catch rate vs scrape cost (3.61 vs 0.40). <a href="https://github.com/redevops-io/context-runtime" style="color:#4fd1c5;text-decoration:none">learn more \u2192</a></span></div>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    import re as _cr_re
    page = render(fetch_activity())
    if _RADAR_CONSOLE is not None:
        panel = "<section class='shell' style='margin-top:var(--sp-4)'>" + _RADAR_CONSOLE.panel_html("radar") + "</section>"
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

    if action == "add_watch":
        return JSONResponse(_add_watch(body or {}))
    if action == "brief":
        return JSONResponse(_brief(body or {}))
    return JSONResponse(
        {"status": "error", "error": f"unknown action '{action}'",
         "supported": ["add_watch", "brief"]},
        status_code=400,
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive market-radar as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_market_radar_operator
    app.include_router(build_market_radar_operator().router())
except Exception:  # noqa: BLE001 — agentic_os absent → no operator surface
    pass
