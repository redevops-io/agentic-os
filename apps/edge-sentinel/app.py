"""edge-sentinel — agentic SOC module wrapping the running CrowdSec core.

Sibling of agents/billing (the reference vertical slice). Same pattern, new core:
wraps the self-hosted **CrowdSec** instance (the OSS detection/decision engine) with

  * an agent layer that reads REAL CrowdSec data (live decisions over the LAPI bouncer
    API + alerts via `cscli ... -o json`), and
  * an MD3 SOC dashboard (same design tokens as deploy/module_service.py) rendered from
    that live data — no mock data.

Pattern, mapped from billing → edge-sentinel:
  1. point CORE at the running CrowdSec LAPI + a bouncer API key,
  2. `fetch_activity` pulls real decisions + alerts + a `compute_kpis`,
  3. reuse BASE_CSS + the SOC render helpers below,
  4. add agentic actions in /agent/run that are deterministic core calls, with a
     human-approval gate on anything that BLOCKS traffic (block_ip → approve_block).

Endpoints:
  GET  /health        -> {"status","core":"crowdsec","connected": <bool>}
  GET  /api/activity  -> live KPIs + decisions + alerts derived from CrowdSec
  GET  /              -> MD3 SOC dashboard rendered from the live data
  POST /agent/run     -> agentic action:
                           {"action":"block_ip","ip":...}      -> pending_approval
                           {"action":"approve_block","ip":...}  -> real cscli ban
                           {"action":"triage"}                  -> summarize alerts

Config (env; seed.py writes agents/edge-sentinel/.env automatically):
  CROWDSEC_LAPI_URL   LAPI base, default http://localhost:8086
  CROWDSEC_BOUNCER_KEY  X-Api-Key for GET /v1/decisions (from `cscli bouncers add`)
  CROWDSEC_CONTAINER  docker container name, default agentic-cores-crowdsec-1
  CROWDSEC_FRONT_URL  link for the "Open CrowdSec metrics" button
  PORT                uvicorn port, default 8203
  ANTHROPIC_API_KEY   OPTIONAL — if set, `triage` adds an LLM reasoning blurb;
                      the endpoint works fully without it.
"""
from __future__ import annotations

import html
import json
import os

# ── enterprise permissions plane — gate tool/data access by (app, user) ──
# No-op without the context_runtime_enterprise package or CR_PERMISSIONS=1 (open-core default).
try:
    from context_runtime_enterprise.apps import bootstrap as _cr_bootstrap
    _req_principal = _cr_bootstrap("edge-sentinel")
except Exception:  # noqa: BLE001
    def _req_principal(request=None):
        return None
import re
import subprocess
import time
from pathlib import Path

import httpx
try:
    from context_runtime.adapters.model_openai import OpenAICompatibleModel
    from context_runtime.types import ModelRequest
except Exception:  # pragma: no cover - context-runtime optional
    OpenAICompatibleModel = None
    ModelRequest = None
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

_BLURB_MODEL = None
if OpenAICompatibleModel is not None:
    try:
        _BLURB_MODEL = OpenAICompatibleModel.from_env()
    except Exception:  # pragma: no cover - env incomplete
        _BLURB_MODEL = None
else:
    _BLURB_MODEL = None

# --- config ------------------------------------------------------------------
# The CrowdSec LAPI/cscli clients, KPIs and agentic actions live in core.py (pure — no web /
# context-runtime deps) so the Mission Runtime operator can invoke them and they can be
# tested against a fake CrowdSec. Import config + core helpers from there; core loads .env.
from . import core
from . import live  # noqa: E402 — wires the SOC app to the Security Intelligence Core (real cases)
from . import auth  # noqa: E402 — the HTTP-Basic operator gate for real execution
# Self-contained security planes — each degrades gracefully + ships labelled SAMPLE until its token
# is set (netbird/portabase) or reports unavailable until wired (deploy_scan). Their panels reuse the
# page chrome below via a deferred import, so there is no import cycle.
from . import netbird  # noqa: E402 — NetBird zero-trust access review
from . import portabase  # noqa: E402 — Portabase backup/continuity coverage
from . import deploy_scan  # noqa: E402 — runtime-DAST proxy to the deploy-scan MCP service
from . import permissions_ui  # noqa: E402 — live vuln-DB permissions demo
from .core import (  # noqa: E402
    TENANT, SUBTITLE, crowdsec_connected, fetch_activity, _severity,
)

PORT = int(os.environ.get("PORT", "8203"))

app = FastAPI(title="edge-sentinel (Meridian Wealth Management · core: CrowdSec)")


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
.barlist__row{display:grid;grid-template-columns:200px 1fr 56px;align-items:center;gap:var(--sp-4)}
.barlist__label{color:var(--on-surface-variant);font:400 13px/18px var(--font-mono)}
.barlist__pct{text-align:right;font-family:var(--font-mono);font-feature-settings:"tnum";font-size:13px;color:var(--on-surface-variant)}
.footer{color:var(--on-surface-muted);font:400 12px/16px var(--font-sans);text-align:center;padding-top:var(--sp-2)}
.status-banner{display:flex;align-items:center;gap:var(--sp-4);padding:var(--sp-4) var(--sp-5);border-radius:var(--radius-md);border-left:4px solid var(--success);background:var(--success-container);color:var(--on-surface)}
.status-banner--threat{border-left-color:var(--danger);background:var(--danger-container)}
.status-banner__icon{font-size:20px;line-height:1}
.feed{display:flex;flex-direction:column}
.feed__row{display:flex;align-items:center;gap:var(--sp-4);padding:var(--sp-3) 0;border-bottom:1px solid var(--outline-variant)}
.feed__row:last-child{border-bottom:none}
.feed__sev{flex:0 0 auto}
.feed__main{flex:1;min-width:0}
.feed__scenario{font:500 14px/20px var(--font-mono);color:var(--on-surface)}
.feed__meta{font:400 12px/16px var(--font-sans);color:var(--on-surface-muted)}
.feed__src{font-family:var(--font-mono);color:var(--on-surface-variant)}
.feed__time{flex:0 0 auto;font:400 12px/16px var(--font-mono);color:var(--on-surface-muted)}
.mono{font-family:var(--font-mono);font-feature-settings:"tnum"}
"""

FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Roboto:wght@400;500&family=Roboto+Mono:wght@400;500&display=swap">'
)


def _esc(v) -> str:
    return html.escape(str(v))


_SEV_PILL = {
    "critical": "pill--danger",
    "high": "pill--warn",
    "medium": "pill--info",
    "low": "pill--neutral",
}


def _sev_pill(sev: str, label: str | None = None) -> str:
    cls = _SEV_PILL.get(sev, "pill--neutral")
    return f"<span class='pill {cls}'><span class='pill__dot'></span>{_esc(label or sev.upper())}</span>"


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


def _status_banner(data: dict) -> str:
    """Green 'All systems normal' / red when there's an active threat being handled."""
    if not data["connected"]:
        return (
            "<div class='status-banner status-banner--threat'>"
            "<span class='status-banner__icon'>!</span>"
            "<span class='pill pill--danger'><span class='pill__dot'></span>CORE UNREACHABLE</span>"
            "<span class='body-m'>CrowdSec LAPI is not responding — detections cannot be read.</span>"
            "</div>"
        )
    if data["has_threat"]:
        crit = [a for a in data["alerts"] if a["severity"] == "critical"]
        n = len(data["decisions"])
        top = data["alerts"][0]["scenario"] if data["alerts"] else "—"
        return (
            "<div class='status-banner status-banner--threat'>"
            "<span class='status-banner__icon'>&#9888;</span>"
            f"<span class='pill pill--danger'><span class='pill__dot'></span>ACTIVE THREATS · {n} blocked</span>"
            f"<span class='body-m'>{len(crit)} critical · agent is enforcing {n} block decision(s) at the edge. "
            f"Latest: <span class='mono'>{_esc(top)}</span>. Sensitive blocks are human-approved.</span>"
            "</div>"
        )
    return (
        "<div class='status-banner'>"
        "<span class='status-banner__icon'>&#10003;</span>"
        "<span class='pill pill--success'><span class='pill__dot'></span>All systems normal</span>"
        "<span class='body-m'>No active block decisions. CrowdSec is monitoring; the agent is on watch.</span>"
        "</div>"
    )


def _alert_feed(data: dict) -> str:
    """Alert feed grouped by severity with color pills."""
    order = ["critical", "high", "medium", "low"]
    by_sev: dict[str, list[dict]] = {s: [] for s in order}
    for a in data["alerts"]:
        by_sev.setdefault(a["severity"], []).append(a)

    rows = ""
    for sev in order:
        for a in by_sev.get(sev, []):
            rows += (
                "<div class='feed__row'>"
                f"<div class='feed__sev'>{_sev_pill(sev)}</div>"
                "<div class='feed__main'>"
                f"<div class='feed__scenario'>{_esc(a['scenario'])}</div>"
                f"<div class='feed__meta'>from <span class='feed__src'>{_esc(a['source'])}</span> "
                f"· {_esc(a['scope'])} · {_esc(a['events'])} event(s)</div>"
                "</div>"
                f"<div class='feed__time'>{_esc(a['ago'])}</div>"
                "</div>"
            )
    if not rows:
        rows = "<div class='feed__row'><div class='feed__meta'>No alerts in the CrowdSec store.</div></div>"
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Alert feed · by severity</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from CrowdSec</span></div>"
        f"<div class='feed'>{rows}</div>"
        "</div>"
    )


def _scenario_bars(data: dict) -> str:
    body = ""
    for item in data["bars"]:
        body += (
            "<div class='barlist__row'>"
            f"<div class='barlist__label'>{_esc(item['label'])}</div>"
            f"<div class='bar'><span style='width:{int(item['pct'])}%'></span></div>"
            f"<div class='barlist__pct'>{_esc(item['count'])}</div>"
            "</div>"
        )
    if not body:
        body = "<div class='barlist__label'>No scenarios recorded.</div>"
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Detections by scenario (live)</h2></div>"
        f"<div class='barlist'>{body}</div>"
        "</div>"
    )


def _decisions_table(data: dict) -> str:
    rows = ""
    for d in data["decisions"]:
        rows += (
            "<tr>"
            f"<td class='mono'>{_esc(d['value'])}</td>"
            f"<td>{_esc(d['scope'])}</td>"
            f"<td class='mono'>{_esc(d['scenario'])}</td>"
            f"<td>{_sev_pill(d['severity'])}</td>"
            f"<td class='mono'>{_esc(d['duration'])}</td>"
            f"<td><span class='pill pill--danger'>{_esc(d['type'])}</span></td>"
            "</tr>"
        )
    if not rows:
        rows = "<tr><td colspan='6'>No active decisions.</td></tr>"
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Attack sources · active decisions</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>GET /v1/decisions</span></div>"
        "<table class='table'><thead><tr>"
        "<th>Source</th><th>Scope</th><th>Scenario</th><th>Severity</th><th>Expires in</th><th>Action</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "</div>"
    )


# ─── generic server-rendered panel chrome (shared by the NetBird / Portabase plane panels) ───
# Reusable UI utilities — siblings of `_esc`/`_sev_pill`. The plane modules (netbird.py /
# portabase.py) call these via a deferred `from . import app`, so a full page renders with the
# same MD3 tokens as the SOC dashboard without duplicating CSS or creating an import cycle.
def _panel_shell(title: str, subtitle: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} — {_esc(TENANT)}</title>{FONT_LINK}
<style>{BASE_CSS}{PAGE_CSS}</style></head>
<body class="page"><div class="shell">
<header class="appbar"><div class="appbar__row"><h1>{_esc(title)}</h1><span class="spacer"></span>
<a class="btn" href="/">&#8592; SOC dashboard</a></div>
<div class="appbar__tenant"><b>{_esc(TENANT)}</b></div>
<div class="appbar__sub">{_esc(subtitle)}</div></header>
{body}
<footer class="footer">edge-sentinel · redevops.io Agentic Business OS</footer>
</div></body></html>"""


def _live_badge(is_live: bool, hint: str) -> str:
    if is_live:
        return "<span style='background:#0d1f1c;color:#4fd1c5;border:1px solid #4fd1c5;border-radius:5px;padding:2px 9px;font-size:11px;font-weight:700'>LIVE</span>"
    return f"<span style='background:#241f14;color:#e0b000;border:1px solid #6b5a1e;border-radius:5px;padding:2px 9px;font-size:11px;font-weight:700'>SAMPLE — {_esc(hint)}</span>"


def _tbl(headers: list[str], rows: list[list[str]]) -> str:
    h = "".join(f"<th style='text-align:left;padding:7px 10px;color:#9b99a1;font-weight:600;border-bottom:1px solid #2f2f33'>{_esc(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td style='padding:7px 10px;border-bottom:1px solid #1f1f22'>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table style='width:100%;border-collapse:collapse;font-size:13px'><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>"


def _risk_list(risks: list[dict]) -> str:
    if not risks:
        return "<p style='color:#4fd1c5;margin:8px 0'>&#10003; No risks found — least-privilege holds.</p>"
    items = "".join(f"<li style='margin:6px 0'>{_sev_pill(r['severity'])} {_esc(r['detail'])}</li>" for r in risks)
    return f"<ul style='list-style:none;padding:0;margin:8px 0'>{items}</ul>"


def _card(title: str, inner: str) -> str:
    return (f"<section class='shell' style='margin-top:var(--sp-4)'><div class='section-label'>{_esc(title)}</div>"
            f"<div style='background:#151517;border:1px solid #2f2f33;border-radius:12px;padding:14px;overflow-x:auto'>{inner}</div></section>")


def render(data: dict) -> str:
    connected = data["connected"]
    conn_txt = "core: CrowdSec connected" if connected else "core: CrowdSec UNREACHABLE"
    conn_cls = "pill--success" if connected else "pill--danger"
    status_pill = (
        f"<span class='pill {conn_cls}'><span class='pill__dot'></span>agent active · {_esc(conn_txt)}</span>"
    )
    live_badge = "<span class='pill pill--info'><span class='pill__dot'></span>data: live from CrowdSec</span>"
    open_btn = (
        f"<a class='btn' href='{_esc(data['front_url'])}' target='_blank' rel='noopener' "
        "title='CrowdSec has no rich UI — this is the LAPI endpoint; CrowdSec is CLI-managed via cscli'>"
        "Open CrowdSec metrics &#8599;</a>"
    )

    body = (
        _status_banner(data)
        + _kpi_tiles(data["kpis"])
        + "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Threat activity</div>"
        "<div class='grid'>"
        f"<div class='col-6'>{_alert_feed(data)}</div>"
        f"<div class='col-6'>{_scenario_bars(data)}</div>"
        "</div></section>"
        + "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Attack sources &amp; decisions</div>"
        "<div class='grid'>"
        f"<div class='col-12'>{_decisions_table(data)}</div>"
        "</div></section>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edge Sentinel — {_esc(TENANT)}</title>
{FONT_LINK}
<style>{BASE_CSS}{PAGE_CSS}</style>
</head>
<body class="page">
<div class="shell">
  <header class="appbar">
    <div class="appbar__row">
      <h1>Edge Sentinel</h1>
      {status_pill}
      {live_badge}
      <span class="spacer"></span>
      {open_btn}
    </div>
    <div class="appbar__tenant"><b>{_esc(TENANT)}</b> · core: CrowdSec (open-source detection &amp; response, CLI-managed)</div>
    <div class="appbar__sub">{_esc(SUBTITLE)}</div>
  </header>
  {body}
  <footer class="footer">edge-sentinel · live activity for {_esc(TENANT)} ·
    <a href="/api/activity">/api/activity</a> · agent + human, on a real CrowdSec core · redevops.io Agentic Business OS</footer>
</div>
</body>
</html>"""


# --- investigation cases (the REAL case/evidence workspace, built from live alerts) ----------
_CASE_STATUS_PILL = {
    "CONTAINED": "pill--success", "RESOLVED": "pill--success", "AWAITING_APPROVAL": "pill--warn",
    "INVESTIGATING": "pill--info", "OPEN": "pill--neutral", "CLOSED": "pill--neutral",
}


def _case_status_pill(status: str) -> str:
    cls = _CASE_STATUS_PILL.get(status, "pill--neutral")
    return f"<span class='pill {cls}'><span class='pill__dot'></span>{_esc(status)}</span>"


def _cases_panel(summaries: list[dict]) -> str:
    """Render the live investigation cases as a real case/evidence workspace. Every row is a replayable
    SecurityCase derived from immutable CrowdSec evidence — never a canned scenario."""
    rows = ""
    for s in summaries:
        act = s.get("open_action")
        if act and act.get("state") == "AWAITING_APPROVAL":
            rid = act["request_id"]
            ip = act.get("parameters", {}).get("ip", "")
            action = (
                f"<button class='btn' onclick=\"esRespond('{_esc(s['case_id'])}','{_esc(rid)}',true)\">"
                f"Approve block {_esc(ip)}</button> "
                f"<button class='btn btn--ghost' onclick=\"esRespond('{_esc(s['case_id'])}','{_esc(rid)}',false)\">Reject</button>"
            )
        elif act and act.get("state") == "APPROVED":
            rid = act["request_id"]
            action = (
                "<span class='pill pill--warn'><span class='pill__dot'></span>approved · awaiting operator</span> "
                f"<button class='btn' onclick=\"esExecute('{_esc(s['case_id'])}','{_esc(rid)}')\">"
                "Execute (operator) &#128274;</button>"
            )
        else:
            action = "<span class='mono' style='color:#9b99a1'>—</span>"
        c = s["counts"]
        rows += (
            "<tr>"
            f"<td>{_sev_pill(s['severity'])}</td>"
            f"<td><a class='mono' href='/api/cases/{_esc(s['case_id'])}' target='_blank' rel='noopener' "
            f"title='the full evidence→observation→finding→action bundle'>{_esc(s['title'])}</a></td>"
            f"<td>{_case_status_pill(s['status'])}</td>"
            f"<td class='mono'>{c['evidence']}e · {c['findings']}f · {c['decisions']}d</td>"
            "<td><span class='pill pill--success'><span class='pill__dot'></span>replayable</span></td>"
            f"<td>{action}</td>"
            "</tr>"
        )
    if not rows:
        rows = ("<tr><td colspan='6'>No investigation cases yet — a case is opened from each live CrowdSec "
                "alert as it arrives. Nothing here is canned.</td></tr>")
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Investigation cases · evidence workspace</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>GET /api/cases</span></div>"
        "<div class='body-m' style='color:#9b99a1;margin:-4px 0 10px'>Each row is a replayable "
        "<span class='mono'>SecurityCase</span> built from immutable evidence — raw alert &rarr; observation "
        "&rarr; finding &rarr; approval-gated action. <b>Approve is open</b> (it only stages the action); the "
        "<b>real kick-off is operator-authenticated</b> and dry-run by default — the edge is never touched on "
        "approve or reject.</div>"
        "<table class='table'><thead><tr>"
        "<th>Severity</th><th>Case</th><th>Status</th><th>Evidence</th><th>Replay</th><th>Response</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "<script>\n"
        "async function esRespond(cid,rid,approve){\n"
        "  const r = await fetch(`/api/cases/${cid}/respond`,{method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({request_id:rid,approved:approve,actor:'demo-approver'})});\n"
        "  const j = await r.json();\n"
        "  if(approve){alert('Approved and staged. Real execution needs an authenticated operator — "
        "click \\u201cExecute (operator)\\u201d.');}\n"
        "  else{alert('Rejected — the edge was not touched.');}\n"
        "  location.reload();\n"
        "}\n"
        "async function esExecute(cid,rid){\n"
        "  // Browser issues the HTTP Basic challenge; operator creds gate the REAL kick-off.\n"
        "  const r = await fetch(`/api/cases/${cid}/execute`,{method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({request_id:rid})});\n"
        "  if(r.status===401){alert('Operator authentication required (HTTP Basic).');return;}\n"
        "  if(r.status===503){alert('Operator approval is not configured on this deployment.');return;}\n"
        "  const j = await r.json();\n"
        "  alert(j.dry_run ? 'DRY-RUN: edge not touched (SENTINEL_BLOCK_ENABLED unset). Receipt: '+"
        "(j.receipt?j.receipt.status:'(none)') : 'Executed. Receipt: '+(j.receipt?j.receipt.status:'(none)')+"
        "' · verified='+(j.verification?j.verification.verified:'n/a'));\n"
        "  location.reload();\n"
        "}\n"
        "</script>"
        "</div>"
    )


# --- optional LLM reasoning blurb (guarded: works without any API key) -------
def _llm_blurb(prompt: str) -> str | None:
    """Return a one-line reasoning blurb from the model plane, or None on failure."""
    def _fallback() -> str | None:
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
    if _BLURB_MODEL is None or ModelRequest is None:
        return _fallback()
    try:
        res = _BLURB_MODEL.complete(ModelRequest(messages=({"role": "user", "content": prompt},), max_tokens=220))
        return res.text
    except Exception:
        return _fallback()

# --- agentic actions ---------------------------------------------------------
def _block_ip(body: dict) -> dict:
    """Blocking traffic is the SENSITIVE action — never auto-executed.

    Mirrors the module's `approval_required:[remediation]`: stage the block and return
    pending_approval. The actual ban only happens via the `approve_block` action.
    """
    ip = (body.get("ip") or "").strip()
    if not ip:
        return {"status": "error", "action": "block_ip", "error": "missing 'ip'"}
    return {
        "status": "pending_approval",
        "action": "block_ip",
        "ip": ip,
        "requires": "human approval",
        "summary": f"block {ip}",
        "detail": (
            f"Edge block of {ip} is staged and awaiting human approval. "
            "Blocks are never auto-enforced by the agent — call approve_block to enforce."
        ),
    }


def _approve_block(body: dict) -> dict:
    """The approved path: enforce the ban decision on the real CrowdSec core — but only when
    SENTINEL_BLOCK_ENABLED is set. Default is dry-run, so an approval in the public demo never mutates the
    live edge by accident (the same safety flag the case-execute path honors).

    Thin wrapper over core.block_ip (POST /v1/decisions) — core stays context-runtime-free.
    """
    ip = ((body or {}).get("ip") or "").strip()
    if not auth.block_enabled():
        return {"status": "dry_run", "action": "approve_block", "ip": ip,
                "summary": f"DRY-RUN: would block {ip}",
                "detail": "SENTINEL_BLOCK_ENABLED not set — the edge was not touched."}
    return core.block_ip(body or {})


def _unblock(body: dict) -> dict:
    """Compensating action (saga undo) for _approve_block — lift the ban."""
    return core.unblock_ip(body or {})


def _triage(body: dict) -> dict:
    """Summarize the current alerts/decisions over the real CrowdSec core, with the
    optional LLM narration blurb (core stays context-runtime-free)."""
    return core.triage(blurb=_llm_blurb)


# ──────── conversational agent · Context Runtime (LLM) + redevops-rag (grounding) + agent-harness (tools) ────────
try:
    from context_runtime.integrations.agent_console import AgentConsole as _AgentConsole, tool as _crtool
except Exception:  # noqa: BLE001 — context-runtime absent → no console
    _AgentConsole = None

_SENTINEL_PRIMER = """CrowdSec is the open-source engine watching your network. It reads your logs, spots attacks, and can block the attacker's IP address at the edge. Think of it as a smart doorman: it recognises bad behaviour and shuts the door on it. edge-sentinel is the friendly agent layer on top — you can ask it what's happening, and it always keeps a human in the loop before it blocks anyone.

The LAPI (Local API) is CrowdSec's brain: it holds the list of who should be blocked and hands that list out. A BOUNCER is the muscle — the component that actually enforces a block (in a firewall, web server, or proxy) by asking the LAPI "should I let this IP in?". Detection and enforcement are separate on purpose.

A DECISION is an instruction to block a source, almost always a BAN on an IP address for a set DURATION (e.g. 4h). "Active decisions" = who is being blocked right now. When the duration runs out the block lifts automatically. To ban an IP for real, the agent runs `cscli decisions add`; to lift one you run `cscli decisions delete --ip <address>` (this is how you UNBAN someone).

An ALERT is CrowdSec saying "I detected an attack." Read an alert by its SCENARIO (what the attacker did), its SOURCE (the IP it came from), and its event count (how many suspicious requests). One alert usually leads to one decision, but not always — some alerts are just logged for awareness.

A SCENARIO is the named pattern of bad behaviour that triggered the alert. http-bruteforce means someone hammered a login/form trying many passwords; ssh-bf (SSH brute-force) is the same against SSH logins; http-probing / http-path-traversal means someone poked around looking for weak spots; port scans map out open doors. Brute-force and exploit scenarios are the ones to take seriously.

A PARSER turns raw log lines into structured events; a COLLECTION is a ready-made bundle of parsers and scenarios for a given service (nginx, ssh, etc.) that you install so CrowdSec knows what to look for. You rarely touch these day to day — they're the detection recipes.

A WHITELIST tells CrowdSec to never block certain trusted IPs (your office, your VPN, a monitoring service). A FALSE POSITIVE is a legitimate visitor wrongly flagged — if a real customer or your own address gets blocked, unban them and add them to a whitelist so it doesn't recur. When unsure, prefer a short ban over a permanent one.

Should you worry? A handful of brute-force or probing alerts from random internet IPs is normal background noise — CrowdSec blocked them, nothing to do. Worry when you see many events from one source, repeated attempts on the same account, or an exploit/RCE scenario — those deserve a closer look. edge-sentinel never auto-blocks: sensitive blocks are staged and wait for your approval.

Beyond the network, edge-sentinel also inspects the SOFTWARE SUPPLY CHAIN — the open-source libraries and images this platform installs — BEFORE they run for clients. A supply-chain SCAN (Trivy) checks every dependency, container image and config for known VULNERABILITIES (CVEs), leaked SECRETS and MISCONFIGURATIONS, and builds an SBOM (a Software Bill of Materials: the full list of components). Each CVE names the package, its SEVERITY (critical/high/medium/low), and the FIXED VERSION to upgrade to — the "patched pin" to move to. This is the PRE-DEPLOYMENT half of security; the network monitoring above is the RUNTIME half. Ask edge-sentinel to "scan our dependencies", list the vulnerabilities, explain a CVE, or produce an SBOM — it inspects the software before you trust it.

NETWORK ACCESS (NetBird) is the "who can reach what" half of security — a private WireGuard network with central policies, single sign-on and device checks. CrowdSec guards the perimeter; NetBird controls internal access. edge-sentinel can REVIEW it — ask "review network access" or "check device posture" and it lists the peers (devices) and policies, flagging any over-broad "All-to-All" rule that lets everything reach everything (that breaks least-privilege), plus posture issues like expired logins or unapproved devices. It can also PROPOSE a tighter policy, but like a block it is staged for your approval and only applied when you approve it. HOW TO ENABLE LIVE DATA: create a NetBird personal-access token and set NETBIRD_API_TOKEN (for a self-hosted NetBird also set NETBIRD_API_URL to https://your-netbird-host/api). Until a token is set the review shows clearly-labelled SAMPLE data so you can see the shape; once set it reads your real peers and policies.

BACKUPS / CONTINUITY (Portabase) answer "is every database safely backed up, and could we recover?". Portabase is a self-hosted backup control plane: small agents sit next to each database, take an ENCRYPTED dump, and ship it to one or more STORAGE DESTINATIONS — a local disk for a fast restore, and an OFFSITE S3 bucket (AWS S3 or MinIO) for durability if a whole host is lost. edge-sentinel reports COVERAGE — ask "are the databases backed up?" and it says which cores have a recent, encrypted, offsite backup and where the gaps are (no backup at all, local-only with no offsite copy, or unencrypted). HOW TO SET UP OFFSITE S3 BACKUPS: bring up the backup control plane, add each database as a source and an S3 storage destination (keep the real keys in Vault), enrol the backup agent, then set PORTABASE_API_TOKEN so edge-sentinel can read coverage. Tip: add BOTH a local and an S3 destination and fan each backup out to both. Until a token is set, coverage shows clearly-labelled SAMPLE data.

RUNTIME EXPOSURE (deploy-scan) is the complement of the supply-chain scan: it actively scans a LIVE deployment (a host or URL) for exploitable weakness using nmap / nuclei / ZAP. Supply-chain scanning inspects code & packages BEFORE deploy; this is the RUNTIME half. Because it sends probe traffic it is destructive, so starting a scan is staged for your approval, and scope is fail-closed to an allowlist on the deploy-scan side. Ask to "scan a running deployment for exposure"; it is available once DEPLOY_SCAN_MCP_URL points at the deploy-scan service."""


def _t_threat_summary(_a: dict) -> dict:
    d = fetch_activity()
    if not d["connected"]:
        return {"text": "CrowdSec core is unreachable, so I can't read live detections right now.", "data": d}
    k = {x["label"]: x["value"] for x in d["kpis"]}
    c = d["counts"]
    top = ", ".join(f"{s['scenario']} ({s['count']})" for s in d["top_scenarios"][:3]) or "none"
    return {"text": f"{k.get('Threats blocked')} active block(s) enforced, {k.get('Alerts (24h)')} alert(s) in the last 24h "
            f"({c['alerts']} total in store) from {c['sources']} unique source IP(s). Top scenarios: {top}.",
            "data": {"kpis": d["kpis"], "top_scenarios": d["top_scenarios"], "counts": c}}


def _t_list_bans(_a: dict) -> dict:
    d = fetch_activity()
    bans = d["decisions"]
    if not bans:
        return {"text": "No active block decisions right now — nobody is currently banned.", "data": []}
    lines = [f"{b['value']} · {b['scenario']} · {b['severity']} · {b['type']} expires in {b['duration'] or 'n/a'}" for b in bans[:15]]
    return {"text": f"{len(bans)} active ban(s):\n" + "\n".join(lines), "data": bans}


def _t_explain_alert(a: dict) -> dict:
    q = (a.get("scenario") or a.get("alert") or "").lower().strip()
    d = fetch_activity()
    pool = d["alerts"] + d["decisions"]
    match = None
    if q:
        for r in pool:
            if q in (r.get("scenario_full") or "").lower() or q in (r.get("scenario") or "").lower():
                match = r
                break
    if match is None and not q and pool:
        match = pool[0]  # no scenario named → explain the most notable current one
    if match is None and not q:
        return {"text": "There are no alerts or decisions to explain right now — the store is quiet.", "data": []}
    scenario = (match.get("scenario_full") or match.get("scenario")) if match else q
    sev = (match.get("severity") if match else None) or _severity(scenario)
    s = scenario.lower()
    if "bruteforce" in s or "-bf" in s or "brute" in s or "credential" in s:
        meaning = "someone tried many passwords in a row against a login — a brute-force attempt."
    elif "traversal" in s or "probing" in s or "probe" in s:
        meaning = "someone poked at your web app looking for weak or hidden paths — reconnaissance/probing."
    elif "injection" in s or "rce" in s or "exploit" in s:
        meaning = "someone attempted to exploit a vulnerability to run code or read data — a serious attack attempt."
    elif "scan" in s or "port" in s:
        meaning = "someone scanned for open services — mapping your exposed doors before a possible attack."
    else:
        meaning = "suspicious activity CrowdSec recognised as matching a known bad pattern."
    worry = ("Take this seriously and review the source." if sev in ("critical", "high")
             else "This is common internet background noise; CrowdSec handled it — no action needed unless it repeats.")
    src = (match.get("value") or match.get("source")) if match else None
    where = f" from {src}" if src else ""
    return {"text": f"Scenario {scenario} ({sev}){where}: {meaning} {worry}",
            "data": {"scenario": scenario, "severity": sev, "source": src}}


def _t_ban_ip(a: dict) -> dict:
    # side_effecting + NOT in allow_side_effects → the harness stages this for human approval
    # instead of running it. If approval is granted, _approve_block enforces via cscli.
    return {"text": _approve_block(a or {}).get("summary", "Ban request staged for human approval."),
            "data": {"ip": (a or {}).get("ip", ""), "duration": (a or {}).get("duration", "4h")}}


# ─── Supply-chain inspection (Trivy/Syft) — the PRE-DEPLOY half of Security & Compliance ───
# "Inspect the open source we ship before clients run it." Scans dependencies/images/IaC for
# CVEs, secrets and misconfig, and builds an SBOM. Degrades gracefully until trivy is installed.
try:
    from context_runtime.integrations.supply_chain import SupplyChainScanner as _SCScanner
    _SCAN = _SCScanner()
except Exception:  # noqa: BLE001
    _SCAN = None

# Consumer of the shared NVD/OSV vuln DB (Apache Doris) — enriches Trivy findings against our own
# permissioned store. The principal (role/scope) comes from env, so the same query demonstrates
# row-scope + column masking.
try:
    from context_runtime.integrations.vuln_db import VulnDB as _VulnDB, Principal as _Principal
    _VDB = _VulnDB()
except Exception:  # noqa: BLE001
    _VDB = None
    _Principal = None


def _principal(role: str = "", scope: str = ""):
    if _Principal is None:
        return None
    role = (role or os.environ.get("VULN_DB_ROLE", "security")).lower()
    if role in ("admin", "security"):
        return _Principal(roles=frozenset({role}))
    owns = frozenset(s.strip() for s in (scope or os.environ.get("VULN_DB_SCOPE", "osv")).split(",") if s.strip())
    return _Principal(roles=frozenset({role or "analyst"}), owns_rows_of=owns)


# Default scan = the container's own rootfs (OS + installed language deps = the full supply chain
# we ship). A specific path/image can be passed to scan just that.
SUPPLY_CHAIN_TARGET = os.environ.get("SUPPLY_CHAIN_TARGET", "/")
_SC_CACHE: dict = {"result": None}


def _sc_run(target: str):
    if target in ("/", "", "rootfs"):
        return _SCAN.scan_rootfs("/")
    return _SCAN.scan_fs(target)


def _sc_scan(a: dict) -> dict:
    if _SCAN is None:
        return {"text": "Supply-chain scanning is unavailable (context-runtime missing).", "data": {}}
    target = (a or {}).get("target") or SUPPLY_CHAIN_TARGET
    r = _sc_run(target)
    _SC_CACHE["result"] = r
    if not r.ok:
        return {"text": f"Couldn't scan {target}: {r.note}", "data": r.summary()}
    s = r.summary()
    by = ", ".join(f"{k.lower()}: {v}" for k, v in s["by_severity"].items()) or "no known CVEs"
    return {"text": f"Scanned {target}: {s['total']} vulnerabilit(ies) ({by}); {s['fixable']} fixable, "
            f"{s['secrets']} secret(s), {s['misconfigs']} misconfig(s).", "data": s}


def _sc_list(_a: dict) -> dict:
    r = _SC_CACHE["result"]
    if (r is None or not r.ok) and _SCAN is not None:
        r = _sc_run(SUPPLY_CHAIN_TARGET)
        _SC_CACHE["result"] = r
    if r is None or not r.ok:
        return {"text": "No scan results yet — run a supply-chain scan first (trivy must be installed).", "data": []}
    top = r.findings[:12]
    lines = [f"{f.severity} · {f.id} · {f.pkg} {f.installed}" + (f" → fix {f.fixed}" if f.fixed else " (no fix yet)") for f in top]
    return {"text": f"{len(r.findings)} finding(s); worst {len(top)}:\n" + "\n".join(lines), "data": [f.__dict__ for f in top]}


def _sc_explain(a: dict) -> dict:
    cve = (a.get("id") or a.get("cve") or "").strip().upper()
    r = _SC_CACHE["result"]
    if r is None or not r.ok or not cve:
        return {"text": "Give me a CVE id from the latest scan (run 'scan our dependencies' first).", "data": {}}
    m = next((f for f in r.findings if f.id.upper() == cve), None)
    if not m:
        return {"text": f"{cve} isn't in the latest scan of {r.target}.", "data": {}}
    fix = f"Upgrade {m.pkg} from {m.installed} to {m.fixed}." if m.fixed else f"No fixed version yet for {m.pkg} — monitor upstream."
    return {"text": f"{m.id} — {m.severity} in {m.pkg} {m.installed} ({m.target}). {m.title}. {fix}", "data": m.__dict__}


def _sc_sbom(_a: dict) -> dict:
    if _SCAN is None:
        return {"text": "SBOM unavailable (context-runtime missing).", "data": {}}
    b = _SCAN.sbom(SUPPLY_CHAIN_TARGET)
    if not b.get("ok"):
        return {"text": b.get("note", "Couldn't build an SBOM."), "data": b}
    return {"text": f"SBOM ({b['tool']}): {b['components']} components. Sample: "
            + ", ".join(f"{c['name']}@{c.get('version', '?')}" for c in b["sample"][:8]), "data": b}


def _sc_status(_a: dict) -> dict:
    if _SCAN is None:
        return {"text": "Scanners unavailable (context-runtime missing).", "data": {}}
    av = _SCAN.available()
    have = ", ".join(k for k, v in av.items() if v) or "none installed yet"
    r = _SC_CACHE["result"]
    last = f" Last scan of {r.target}: {r.summary()['total']} findings." if (r and r.ok) else " No scan run yet."
    return {"text": f"Supply-chain scanners present: {have}.{last}", "data": {"available": av}}


# ─── Runtime data plane: ClamAV (malware) + Wazuh (SIEM/XDR hub) — now deployed alongside ───
CLAMAV_HOST = os.environ.get("CLAMAV_HOST", "security-clamav")
CLAMAV_PORT = int(os.environ.get("CLAMAV_PORT", "3310"))
WAZUH_API = os.environ.get("WAZUH_API_URL", "https://host.docker.internal:55000").rstrip("/")
WAZUH_USER = os.environ.get("WAZUH_API_USER", "wazuh-wui")
WAZUH_PASS = os.environ.get("WAZUH_API_PASSWORD", "")


def _clamd(cmd: str, timeout: float = 30.0) -> str:
    import socket
    with socket.create_connection((CLAMAV_HOST, CLAMAV_PORT), timeout=timeout) as s:
        s.sendall(("n" + cmd + "\n").encode())
        s.settimeout(timeout)
        chunks = []
        try:
            while True:
                b = s.recv(4096)
                if not b:
                    break
                chunks.append(b)
        except Exception:  # noqa: BLE001
            pass
    return b"".join(chunks).decode("utf-8", "ignore").strip()


def _t_malware_scan(a: dict) -> dict:
    # clamd sees the stack at /scan (host /projects mounted read-only in the clamav container).
    path = (a or {}).get("path") or "/scan"
    try:
        if _clamd("PING") != "PONG":
            return {"text": "ClamAV is not responding.", "data": {}}
        out = _clamd("SCAN " + path, timeout=180.0)
    except Exception as e:  # noqa: BLE001
        return {"text": f"ClamAV unreachable ({CLAMAV_HOST}:{CLAMAV_PORT}): {e}", "data": {}}
    infected = [ln for ln in out.splitlines() if ln.strip().endswith("FOUND")]
    if infected:
        return {"text": f"⚠️ Malware detected ({len(infected)}):\n" + "\n".join(infected[:10]), "data": {"infected": infected}}
    return {"text": f"ClamAV scanned {path}: clean, no malware found.", "data": {"clean": True}}


_WZ_TOKEN: dict = {"t": None}


def _wazuh_get(path: str) -> dict:
    if not WAZUH_PASS:
        return {}
    try:
        if not _WZ_TOKEN["t"]:
            r = httpx.get(WAZUH_API + "/security/user/authenticate", auth=(WAZUH_USER, WAZUH_PASS), verify=False, timeout=10.0)
            _WZ_TOKEN["t"] = r.json()["data"]["token"]
        h = {"Authorization": "Bearer " + _WZ_TOKEN["t"]}
        return httpx.get(WAZUH_API + path, headers=h, verify=False, timeout=10.0).json()
    except Exception:  # noqa: BLE001
        _WZ_TOKEN["t"] = None
        return {}


def _t_wazuh_status(_a: dict) -> dict:
    d = _wazuh_get("/agents/summary/status")
    if not d:
        return {"text": "The Wazuh SIEM hub isn't reachable from here yet (set WAZUH_API_PASSWORD). "
                "Once wired, it unifies malware (ClamAV), compliance (OpenSCAP), FIM, SCA and vulnerability detection.", "data": {}}
    data = (d.get("data") or {})
    c = data.get("connection", data)   # /agents/summary/status nests counts under .connection
    active, disc = c.get("active", 0), c.get("disconnected", 0)
    total = c.get("total", active + disc)
    return {"text": f"Wazuh SIEM/XDR hub is up: {active} active agent(s), {disc} disconnected, {total} enrolled. "
            f"It correlates malware (ClamAV), compliance (OpenSCAP), FIM, SCA and vulnerability signals in one place. "
            + ("Enroll endpoints as Wazuh agents to populate it." if total == 0 else ""),
            "data": c}


# ─── Per-app supply-chain scanning — cover EVERY agentic app's open-source dependencies ───
def _resolve_app_container(app: str) -> str:
    raw = (app or "").strip().lower()
    if not raw or _SCAN is None:
        return ""
    names = [c["name"] for c in _SCAN.list_scannable_containers("")]
    # tolerate natural phrasing: "the books app", "agentic-billing", "market radar container"
    cleaned = re.sub(r"\b(the|an?|app|application|container|service|agentic)\b", " ", raw)
    tokens = [t for t in re.split(r"[\s_./-]+", cleaned) if t and t not in ("os", "stack")]
    forms = [raw.replace(" ", "-"), cleaned.strip().replace(" ", "-"), *tokens]
    for form in forms:  # exact agentic app container first
        for n in names:
            if n in (f"agentic-os-stack-{form}-1", f"agentic-os-stack-agentic-{form}-1"):
                return n
    for form in forms:  # then substring, preferring agentic app containers
        cand = [n for n in names if form and form in n.lower()]
        cand.sort(key=lambda n: (0 if n.startswith("agentic-os-stack-") else 1, len(n)))
        if cand:
            return cand[0]
    return ""


def _t_scan_app(a: dict) -> dict:
    if _SCAN is None:
        return {"text": "Supply-chain scanning is unavailable (context-runtime missing).", "data": {}}
    a = a or {}
    # tolerate the various keys an LLM picks (app / app_name / name / target / value)
    app = next((str(a[k]) for k in ("app", "app_name", "name", "target", "value", "application") if a.get(k)), "")
    cont = _resolve_app_container(app)
    if not cont:
        avail = ", ".join(c["name"].replace("agentic-os-stack-", "").replace("-1", "")
                          for c in _SCAN.list_scannable_containers("agentic-os-stack")[:20])
        return {"text": f"I couldn't find a container matching '{app}'. Scannable apps: {avail}.", "data": {}}
    r = _SCAN.scan_container(cont)
    _SC_CACHE["result"] = r
    if not r.ok:
        return {"text": f"Couldn't scan {cont}: {r.note}", "data": r.summary()}
    t = _SCAN.triage(r, top=6)
    adv = _SCAN.advise(r)   # OS-base vs app-dependency split → actionable recommendation
    enrich = {}
    if _VDB is not None:
        try:
            enrich = _VDB.enrich(r.findings, _principal())   # cross-reference our NVD/OSV vuln-DB
        except Exception:  # noqa: BLE001
            enrich = {}
    fixes = "\n".join(f"  • {f['action']}  ({f['severity']} · {f['id']})" + (" — ✓ in our vuln-DB" if f["id"] in enrich else "")
                      for f in t["fixes"]) or "  • no fixable CVEs — nothing to upgrade"
    corr = f" {len(enrich)} of these CVEs are corroborated in our NVD/OSV vuln-DB." if enrich else ""
    return {"text": f"Scanned **{cont}** — {t['summary']}.{corr}\nSuggested fixes:\n{fixes}\n\n**Recommendation:** {adv['recommendation']}",
            "data": {"summary": r.summary(), "triage": t, "advice": adv, "vuln_db_matches": {k: len(v) for k, v in enrich.items()}}}


def _t_list_apps(_a: dict) -> dict:
    if _SCAN is None:
        return {"text": "Supply-chain scanning is unavailable.", "data": {}}
    conts = _SCAN.list_scannable_containers("")
    apps = [c["name"] for c in conts if c["name"].startswith("agentic-os-stack-")]
    cores = [c["name"] for c in conts if not c["name"].startswith("agentic-os-stack-")]
    short = [a.replace("agentic-os-stack-", "").replace("-1", "") for a in apps]
    return {"text": f"{len(apps)} agentic app(s) and {len(cores)} core container(s) can be scanned for open-source "
            f"vulnerabilities. Apps: {', '.join(short[:24])}. Ask me to \"scan the <app>\" (or \"scan all apps\") "
            f"and I'll list the CVEs and the fixes to apply.", "data": {"apps": apps, "cores": cores}}


# Ecosystem inference for scan_apps: a Trivy Finding has no explicit ecosystem field, so we match on
# the target (lockfile/path) + os/lang class. Best-effort — enough to answer "which apps have npm CVEs".
_ECO_ALIASES = {
    "npm": ("node", "package-lock", "package.json", "yarn", "pnpm", "npm", "javascript"),
    "node": ("node", "package-lock", "package.json", "yarn", "pnpm", "npm"),
    "pypi": ("python", "requirement", "poetry", "pipfile", "site-packages", ".egg", "pip"),
    "python": ("python", "requirement", "poetry", "pipfile", "site-packages", ".egg", "pip"),
    "pip": ("python", "requirement", "poetry", "pipfile", "site-packages", ".egg", "pip"),
    "maven": ("pom.xml", "gradle", ".jar", "java", "maven"),
    "java": ("pom.xml", "gradle", ".jar", "java", "maven"),
    "go": ("go.sum", "go.mod", "gobinary", "golang"),
    "golang": ("go.sum", "go.mod", "gobinary", "golang"),
    "ruby": ("gemfile", ".gem", "ruby", "bundler"),
    "rust": ("cargo", ".crate", "rust"),
}
_SEV_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}


def _short_cont(name: str) -> str:
    return name.replace("agentic-os-stack-", "").replace("-1", "")


def _eco_match(f, eco: str) -> bool:
    if not eco:
        return True
    if eco in ("os", "system", "debian", "alpine", "ubuntu", "base"):
        return getattr(f, "cls", "") == "os"
    hints = _ECO_ALIASES.get(eco)
    t = (getattr(f, "target", "") or "").lower()
    if hints:
        return any(h in t for h in hints)
    return eco in t or eco in (getattr(f, "title", "") or "").lower()


def _t_scan_apps(a: dict) -> dict:
    """Scan MANY app containers at once and roll up the CVEs, optionally filtered to one ecosystem.
    This is the 'scan all apps' / 'which apps have npm CVEs' capability."""
    if _SCAN is None:
        return {"text": "Supply-chain scanning is unavailable (context-runtime missing).", "data": {}}
    a = a or {}
    eco = str(a.get("ecosystem") or a.get("filter") or a.get("type") or "").strip().lower()
    try:
        limit = int(a.get("limit") or 0)
    except Exception:  # noqa: BLE001
        limit = 0
    limit = limit if limit > 0 else 8

    conts = _SCAN.list_scannable_containers("agentic-os-stack")
    names = [c["name"] for c in conts if c["name"].startswith("agentic-os-stack-")]
    wanted = a.get("apps") or a.get("targets")
    if isinstance(wanted, str):
        wanted = [w.strip() for w in re.split(r"[,\s]+", wanted) if w.strip()]
    if wanted:
        picked = [_resolve_app_container(w) for w in wanted]
        names = [n for n in dict.fromkeys(picked) if n]

    scanned = names[:limit]
    truncated = max(0, len(names) - len(scanned))
    per_app: list[dict] = []
    fixes: dict[str, tuple] = {}
    total = 0
    matched_apps = 0
    for cont in scanned:
        r = _SCAN.scan_container(cont)
        if not r.ok:
            per_app.append({"app": _short_cont(cont), "ok": False, "note": r.note})
            continue
        finds = [f for f in r.findings if _eco_match(f, eco)]
        total += len(finds)
        if finds:
            matched_apps += 1
        by_sev: dict[str, int] = {}
        for f in finds:
            by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
            if f.fixed and f.id not in fixes:
                fixes[f.id] = (f.severity, f"upgrade {f.pkg} → {f.fixed} in {_short_cont(cont)}")
        per_app.append({"app": _short_cont(cont), "ok": True, "cves": len(finds), "by_severity": by_sev})

    ok = sorted((p for p in per_app if p.get("ok")), key=lambda p: -p.get("cves", 0))
    label = f"{eco}-ecosystem " if eco else ""
    head = f"Scanned {len(scanned)} app(s) for {label}vulnerabilities"
    if truncated:
        head += f" (of {len(names)} — raise `limit` or scan a specific app for the rest)"
    per_lines = [
        f"  • {p['app']}: {p['cves']} CVE(s)"
        + (f" ({', '.join(f'{k.lower()} {v}' for k, v in sorted(p['by_severity'].items(), key=lambda kv: _SEV_RANK.get(kv[0], 9)))})" if p["cves"] else "")
        for p in ok
    ] or ["  • none"]
    top = sorted(fixes.items(), key=lambda kv: _SEV_RANK.get(kv[1][0], 9))[:8]
    fix_lines = "\n".join(f"  • {act}  ({sev} · {cid})" for cid, (sev, act) in top) or "  • no fixable CVEs found"
    body = (f"{head}. {matched_apps} app(s) carry {label}CVEs — {total} finding(s) total.\n\n"
            f"Per app (worst first):\n" + "\n".join(per_lines) + f"\n\nTop fixes:\n{fix_lines}")
    return {"text": body, "data": {"ecosystem": eco or None, "scanned": [_short_cont(c) for c in scanned],
            "truncated": truncated, "apps_with_findings": matched_apps, "total": total, "per_app": ok,
            "fixes": [{"id": cid, "severity": sev, "action": act} for cid, (sev, act) in top]}}


# ─── Consume the shared NVD/OSV vuln-DB (Doris) — look up advisories + demo the permissioning ───
def _t_vuln_lookup(a: dict) -> dict:
    if _VDB is None or not _VDB.available():
        return {"text": "Our vulnerability database (Doris) isn't reachable from here.", "data": {}}
    a = a or {}
    pkg = a.get("package") or a.get("pkg") or ""
    cve = a.get("cve") or a.get("id") or a.get("cve_id") or ""
    rows = _VDB.lookup(package=pkg or None, cve=cve or None, principal=_principal())
    if not rows:
        return {"text": f"Our vuln-DB has no records for {('CVE ' + cve) if cve else ('package ' + pkg) or 'that'}.", "data": []}
    lines = [f"{r['cve_id']} · {r['package']} ({r.get('ecosystem') or '—'}) · cvss {r.get('cvss', 0)} → "
             f"fix {r.get('fixed_version') or 'none'}  [{r.get('source')}]" for r in rows[:10]]
    return {"text": f"Our NVD/OSV vuln-DB — {len(rows)} record(s):\n" + "\n".join(lines), "data": rows[:20]}


def _t_vuln_db_permissions(_a: dict) -> dict:
    if _VDB is None or not _VDB.available() or _Principal is None:
        return {"text": "Our vulnerability database (Doris) isn't reachable from here.", "data": {}}
    admin = _VDB.lookup(principal=_Principal(roles=frozenset({"security"})), limit=100000)
    osv = _VDB.lookup(principal=_Principal(roles=frozenset({"analyst"}), owns_rows_of=frozenset({"osv"})), limit=100000)
    nvd = _VDB.lookup(principal=_Principal(roles=frozenset({"analyst"}), owns_rows_of=frozenset({"nvd"})), limit=100000)
    refs_admin = sum(1 for r in admin if r.get("refs"))
    refs_osv = sum(1 for r in osv if r.get("refs"))
    return {"text": ("Permissioning demo — the SAME vuln-DB, different principals:\n"
            f"  • security role: {len(admin)} rows, references visible on {refs_admin}\n"
            f"  • analyst scoped to OSV: {len(osv)} rows, references visible on {refs_osv} (masked)\n"
            f"  • analyst scoped to NVD: {len(nvd)} rows\n"
            "Row scope filters by data owner; the references column is masked for non-privileged roles."),
            "data": {"security": len(admin), "osv_analyst": len(osv), "nvd_analyst": len(nvd),
                     "refs_visible_admin": refs_admin, "refs_visible_osv": refs_osv}}


if _AgentConsole is not None:
    _SENTINEL_CONSOLE = _AgentConsole(
        f"{TENANT} Edge Sentinel", _SENTINEL_PRIMER,
        tools=[
            _crtool("threat_summary", "current threat posture — active bans, alerts in the last 24h, top attack scenarios, source IPs", _t_threat_summary),
            _crtool("list_bans", "list active block decisions: source IP, scenario, severity, and when the ban expires", _t_list_bans),
            _crtool("explain_alert", "explain a specific alert currently in this network's feed (by scenario name) and whether it needs action", _t_explain_alert,
                    parameters={"type": "object", "properties": {"scenario": {"type": "string", "description": "a scenario name from the live feed to explain"}}}),
            _crtool("ban_ip", "block an IP at the edge for a duration (destructive — staged for human approval)", _t_ban_ip, side_effecting=True,
                    parameters={"type": "object", "properties": {"ip": {"type": "string"}, "duration": {"type": "string", "description": "e.g. 4h, 24h"}}}),
            # supply-chain (pre-deploy) tools — read-only inspection, run ungated
            _crtool("supply_chain_scan", "scan our own software supply chain (dependencies, images, IaC, secrets) for known vulnerabilities before deployment", _sc_scan,
                    parameters={"type": "object", "properties": {"target": {"type": "string", "description": "path or image to scan (default: the stack's dependencies)"}}}),
            _crtool("list_vulnerabilities", "list the CVEs found in the last supply-chain scan, worst first, each with the version that fixes it", _sc_list),
            _crtool("explain_vulnerability", "explain a specific CVE from the last scan and the patched version to move to", _sc_explain,
                    parameters={"type": "object", "properties": {"id": {"type": "string", "description": "a CVE id from the scan"}}}),
            _crtool("scan_app", "scan a SPECIFIC agentic app's container image for open-source (CVE) vulnerabilities and suggest the fixes to apply", _t_scan_app,
                    parameters={"type": "object", "properties": {"app": {"type": "string", "description": "the app to scan, e.g. billing, compliance, market-radar, books"}}}),
            _crtool("scan_apps", "scan MANY agentic apps/containers at once for open-source (CVE) vulnerabilities and roll up the results — optionally filtered to ONE package ecosystem (npm, pypi, maven, go, os). Use this for 'scan all apps', 'which apps have npm modules/CVEs', 'scan the npm apps'.", _t_scan_apps,
                    parameters={"type": "object", "properties": {
                        "ecosystem": {"type": "string", "description": "restrict findings to one ecosystem: npm | pypi | maven | go | os (optional)"},
                        "apps": {"type": "string", "description": "comma-separated app names to scan (optional; default: all agentic apps)"},
                        "limit": {"type": "integer", "description": "max apps to scan in one call (default 8)"}}}),
            _crtool("list_scannable_apps", "list every agentic app + core container whose open-source dependencies can be scanned", _t_list_apps),
            _crtool("vuln_db_lookup", "look up a package or CVE in our own NVD/OSV vulnerability database (advisories + patched versions)", _t_vuln_lookup,
                    parameters={"type": "object", "properties": {"package": {"type": "string"}, "cve": {"type": "string", "description": "a CVE id"}}}),
            _crtool("vuln_db_permissions", "demonstrate the vuln-DB access policy: the same query returns different rows/columns per principal (row scope + column masking)", _t_vuln_db_permissions),
            _crtool("sbom", "generate the software bill of materials (component inventory) for the stack", _sc_sbom),
            _crtool("scanner_status", "which supply-chain scanners are installed (trivy/syft/cosign) and the last scan result", _sc_status),
            # runtime-DAST (post-deploy) tools — scan a LIVE deployment via the deploy-scan service
            _crtool("scan_deployment", "actively scan a RUNNING deployment (host or URL) for exploitable exposure with nmap/nuclei/ZAP — the RUNTIME half of security, complementing the supply-chain scan. Destructive: sends probe traffic, so it is staged for human approval. profile: recon (nmap surface) | web-baseline (nuclei + ZAP passive) | web-active (ZAP attack traffic) | full", deploy_scan.scan, side_effecting=True,
                    parameters={"type": "object", "properties": {
                        "target": {"type": "string", "description": "host or URL to scan (must be in the deploy-scan allowlist)"},
                        "profile": {"type": "string", "description": "recon | web-baseline | web-active | full (default web-baseline)"}}}),
            _crtool("scan_status", "check the progress of a running deployment scan by its job id", deploy_scan.status,
                    parameters={"type": "object", "properties": {"job_id": {"type": "string"}}}),
            _crtool("scan_results", "fetch the findings + severity summary of a finished deployment scan by its job id", deploy_scan.results,
                    parameters={"type": "object", "properties": {"job_id": {"type": "string"}}}),
            # runtime data plane
            _crtool("malware_scan", "scan the stack's files for malware/viruses with ClamAV", _t_malware_scan,
                    parameters={"type": "object", "properties": {"path": {"type": "string", "description": "path to scan (default: the whole stack)"}}}),
            _crtool("wazuh_status", "report the Wazuh SIEM/XDR hub — connected agents and what it monitors (malware, compliance, FIM, vulns)", _t_wazuh_status),
            # network access plane (NetBird / zero-trust) — the "who can reach what" half
            _crtool("network_access_review", "review zero-trust network access (NetBird): peers, groups and policies, flagging over-broad All-to-All access that breaks least-privilege", netbird.access_review),
            _crtool("network_posture", "check device posture on the network (NetBird): peers with expired logins, pending/unapproved devices, or broad SSH access", netbird.posture),
            _crtool("propose_network_policy", "draft a tighter NetBird access policy to replace an over-broad one (destructive — staged for human approval, applied only via approve_network_policy)", netbird.propose, side_effecting=True,
                    parameters={"type": "object", "properties": {"policy": {"type": "string", "description": "name of the over-broad policy to tighten (optional; default: the first one found)"}}}),
            # continuity plane (Portabase / backups) — is every core safely backed up offsite?
            _crtool("backup_status", "report backup coverage (Portabase): which of the stack's databases have a recent, encrypted, offsite (S3) backup — and the gaps", portabase.backup_status),
        ],
        suggestions=["Any threats today?", "Scan the billing app for vulnerabilities", "Scan a running deployment for exposure", "Review network access"],
        subtitle="Ask me to scan any app's open-source dependencies, check the network/SIEM, or explain a finding.",
        allow_side_effects=[],  # ban_ip stays gated → the harness asks for human approval before blocking
    )
else:
    _SENTINEL_CONSOLE = None


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "core": "crowdsec", "connected": crowdsec_connected()}


@app.post("/api/agent")
async def api_agent(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if _SENTINEL_CONSOLE is None:
        return JSONResponse({"intent": "help", "text": "The assistant is offline (context-runtime unavailable).", "evidence": []})
    return JSONResponse(_SENTINEL_CONSOLE.respond((body or {}).get("message", ""), principal=_req_principal(request)))


@app.get("/api/activity")
def activity() -> JSONResponse:
    return JSONResponse(fetch_activity())


@app.post("/api/permissions/query")
async def permissions_query(request: Request) -> JSONResponse:
    """Run the vuln query as full-access (left) and the posted scoped grant (right)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return JSONResponse(permissions_ui.query(body or {}))


@app.get("/api/permissions/ui", response_class=HTMLResponse)
def permissions_ui_page() -> str:
    return permissions_ui.PERM_PAGE


@app.get("/api/network/review")
def network_review() -> JSONResponse:
    """Zero-trust network access review over NetBird (live when configured, else sample)."""
    return JSONResponse(netbird.access_review({})["data"])


@app.get("/api/network/ui", response_class=HTMLResponse)
def network_ui() -> str:
    return netbird.panel()


@app.get("/api/backups/status")
def backups_status() -> JSONResponse:
    """Backup coverage over Portabase (live when configured, else sample)."""
    return JSONResponse(portabase.backup_status({})["data"])


@app.get("/api/backups/ui", response_class=HTMLResponse)
def backups_ui() -> str:
    return portabase.panel()


@app.get("/api/cases")
def api_cases() -> JSONResponse:
    """List live investigation cases, worst severity first. Syncs the latest CrowdSec alerts into cases first
    (idempotent), so the list reflects real detections — never a canned scenario."""
    live.sync_cases()
    return JSONResponse({"cases": live.case_summaries()})


@app.get("/api/cases/{case_id}")
def api_case(case_id: str) -> JSONResponse:
    """Full resolved bundle for one case (evidence/observations/findings/actions/decisions/receipts) plus
    the replay proof that the case id is reproducible from its immutable evidence."""
    detail = live.case_detail(case_id)
    if detail is None:
        return JSONResponse({"error": f"unknown case '{case_id}'"}, status_code=404)
    return JSONResponse(detail)


@app.post("/api/cases/{case_id}/respond")
async def api_case_respond(case_id: str, request: Request) -> JSONResponse:
    """DEMO-FACING decision — OPEN, no auth, NO edge effect. Reject records the rejection and stops; approve
    stages the action as APPROVED (awaiting the authenticated execute step). The edge is never touched here —
    the real kick-off is POST /api/cases/{id}/execute, which is operator-authenticated."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    request_id = (body or {}).get("request_id", "")
    if not request_id:
        return JSONResponse({"error": "missing 'request_id'"}, status_code=400)
    try:
        out = live.decide(case_id, request_id,
                          approved=bool((body or {}).get("approved")),
                          actor=(body or {}).get("actor", "demo-approver"))
    except KeyError:
        return JSONResponse({"error": f"unknown case or request ({case_id}/{request_id})"}, status_code=404)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
    return JSONResponse(out)


@app.post("/api/cases/{case_id}/execute")
async def api_case_execute(case_id: str, request: Request,
                           operator: str = Depends(auth.require_operator)) -> JSONResponse:
    """AUTHENTICATED execution — the real kick-off. HTTP-Basic operator gate (503 if unconfigured, 401 if not
    authenticated). Only an already-APPROVED action runs; dry-run unless SENTINEL_BLOCK_ENABLED, so the public
    demo never mutates the live edge by default. Returns the receipt (execution proof) + a distinct verification."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    request_id = (body or {}).get("request_id", "")
    if not request_id:
        return JSONResponse({"error": "missing 'request_id'"}, status_code=400)
    try:
        out = live.execute(case_id, request_id, actor=f"operator:{operator}")
    except KeyError:
        return JSONResponse({"error": f"unknown case or request ({case_id}/{request_id})"}, status_code=404)
    except live.ExecutionError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:  # noqa: BLE001 — e.g. GovernanceError refusing an ungoverned action
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
    return JSONResponse(out)


_CR_BANNER = """<div style="position:sticky;top:0;z-index:9998;background:linear-gradient(90deg,#10201d,#17171a);border-bottom:1px solid #2f2f33;color:#e4e2e6;font:13px/1.4 Roboto,system-ui,sans-serif;padding:9px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap"><span style="background:#4fd1c5;color:#08110f;font-weight:700;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">CONTEXT RUNTIME</span><span style="background:#2f2f33;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">DEMO</span><span style="color:#9b99a1">This app runs on <b style="color:#e4e2e6">Context Runtime</b>, which powers the investigation assistant, supply-chain/vuln scanners and the permissions plane here. Its Edge-Sentinel tenant learns the cheapest alert-source bundle that still reaches the right verdict (0.900 vs 0.800, measured offline). <a href="https://github.com/redevops-io/context-runtime" style="color:#4fd1c5;text-decoration:none">learn more \u2192</a></span><span style="margin-left:auto;display:flex;gap:8px;flex-wrap:wrap"><a href="api/network/ui" style="background:#1b2b28;color:#4fd1c5;border:1px solid #2f6f66;font-weight:700;border-radius:6px;padding:5px 11px;font-size:12px;text-decoration:none;white-space:nowrap">\U0001F310 Network access \u2192</a><a href="api/backups/ui" style="background:#1b2b28;color:#4fd1c5;border:1px solid #2f6f66;font-weight:700;border-radius:6px;padding:5px 11px;font-size:12px;text-decoration:none;white-space:nowrap">\U0001F5C4 Backups \u2192</a><a href="api/permissions/ui" style="background:#4fd1c5;color:#08110f;font-weight:700;border-radius:6px;padding:5px 11px;font-size:12px;text-decoration:none;white-space:nowrap">\U0001F512 Permissions demo \u2192</a></span></div>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    import re as _cr_re
    page = render(fetch_activity())
    blocks = ""
    try:
        live.sync_cases()
        blocks += ("<section class='shell' style='margin-top:var(--sp-4)'>"
                   + _cases_panel(live.case_summaries()) + "</section>")
    except Exception:  # noqa: BLE001 — the case workspace is additive; never break the dashboard
        pass
    if _SENTINEL_CONSOLE is not None:
        blocks += "<section class='shell' style='margin-top:var(--sp-4)'>" + _SENTINEL_CONSOLE.panel_html("sentinel") + "</section>"
    if blocks:
        page = page.replace("<footer", blocks + "<footer", 1)
    page = _cr_re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + _CR_BANNER, page, count=1)
    if "_CR_BANNER" not in page:
        page = _CR_BANNER + page
    return page


@app.post("/agent/run")
async def agent_run(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body or {}).get("action", "")

    # Real kick-off actions are operator-authenticated (HTTP Basic); propose/block-stage/triage stay open.
    if action in ("approve_block", "approve_network_policy"):
        try:
            auth.require_operator(request)
        except HTTPException as e:
            return JSONResponse({"error": e.detail}, status_code=e.status_code,
                                headers=dict(e.headers or {}))

    if action == "block_ip":
        return JSONResponse(_block_ip(body or {}))
    if action == "approve_block":
        return JSONResponse(_approve_block(body or {}))
    if action == "triage":
        return JSONResponse(_triage(body or {}))
    # NetBird — propose a tighter policy (staged) then apply it on approval
    if action == "propose_network_policy":
        return JSONResponse(netbird.propose(body or {}))
    if action == "approve_network_policy":
        if not auth.block_enabled():
            return JSONResponse({"status": "dry_run", "action": "approve_network_policy",
                                 "summary": "DRY-RUN: policy change not applied",
                                 "detail": "SENTINEL_BLOCK_ENABLED not set — NetBird was not modified."})
        return JSONResponse(netbird.approve(body or {}))
    return JSONResponse(
        {"status": "error", "error": f"unknown action '{action}'",
         "supported": ["block_ip", "approve_block", "triage", "propose_network_policy", "approve_network_policy"]},
        status_code=400,
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive edge-sentinel as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_edge_sentinel_operator
    app.include_router(build_edge_sentinel_operator().router())
    # Runtime-native: register this app's governed contract at boot (guarded with the mount above).
    from agentic_os.app_kit.boot import register_app_manifest as _register_runtime_native
    from . import manifest as _runtime_native_manifest
    _register_runtime_native(_runtime_native_manifest)
except Exception as _op_exc:  # agentic_os absent / operator build failed — surface it, do not hide it
    import logging as _logging
    _logging.getLogger("agentic_os.app").error(
        "%s: operator/capability surface NOT mounted (%s: %s) — /invoke + /capabilities are unavailable",
        __name__, type(_op_exc).__name__, _op_exc)
