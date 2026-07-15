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
from fastapi import FastAPI, Request
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
from .core import (  # noqa: E402
    TENANT, SUBTITLE, crowdsec_connected, fetch_activity, _severity,
)

PORT = int(os.environ.get("PORT", "8203"))

app = FastAPI(title="edge-sentinel (Summit Roofing Co. · core: CrowdSec)")


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
    """The approved path: actually enforce the ban decision on the real CrowdSec core.

    Thin wrapper over core.block_ip (POST /v1/decisions) — core stays context-runtime-free.
    """
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

Beyond the network, edge-sentinel also inspects the SOFTWARE SUPPLY CHAIN — the open-source libraries and images this platform installs — BEFORE they run for clients. A supply-chain SCAN (Trivy) checks every dependency, container image and config for known VULNERABILITIES (CVEs), leaked SECRETS and MISCONFIGURATIONS, and builds an SBOM (a Software Bill of Materials: the full list of components). Each CVE names the package, its SEVERITY (critical/high/medium/low), and the FIXED VERSION to upgrade to — the "patched pin" to move to. This is the PRE-DEPLOYMENT half of security; the network monitoring above is the RUNTIME half. Ask edge-sentinel to "scan our dependencies", list the vulnerabilities, explain a CVE, or produce an SBOM — it inspects the software before you trust it."""


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
            f"vulnerabilities. Apps: {', '.join(short[:24])}. Ask me to \"scan the <app>\" and I'll list its CVEs "
            f"and the fixes to apply.", "data": {"apps": apps, "cores": cores}}


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
            _crtool("list_scannable_apps", "list every agentic app + core container whose open-source dependencies can be scanned", _t_list_apps),
            _crtool("vuln_db_lookup", "look up a package or CVE in our own NVD/OSV vulnerability database (advisories + patched versions)", _t_vuln_lookup,
                    parameters={"type": "object", "properties": {"package": {"type": "string"}, "cve": {"type": "string", "description": "a CVE id"}}}),
            _crtool("vuln_db_permissions", "demonstrate the vuln-DB access policy: the same query returns different rows/columns per principal (row scope + column masking)", _t_vuln_db_permissions),
            _crtool("sbom", "generate the software bill of materials (component inventory) for the stack", _sc_sbom),
            _crtool("scanner_status", "which supply-chain scanners are installed (trivy/syft/cosign) and the last scan result", _sc_status),
            # runtime data plane
            _crtool("malware_scan", "scan the stack's files for malware/viruses with ClamAV", _t_malware_scan,
                    parameters={"type": "object", "properties": {"path": {"type": "string", "description": "path to scan (default: the whole stack)"}}}),
            _crtool("wazuh_status", "report the Wazuh SIEM/XDR hub — connected agents and what it monitors (malware, compliance, FIM, vulns)", _t_wazuh_status),
        ],
        suggestions=["Any threats today?", "Scan the billing app for vulnerabilities", "Which apps can you scan?", "What does the SIEM show?"],
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


_CR_BANNER = """<div style="position:sticky;top:0;z-index:9998;background:linear-gradient(90deg,#10201d,#17171a);border-bottom:1px solid #2f2f33;color:#e4e2e6;font:13px/1.4 Roboto,system-ui,sans-serif;padding:9px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap"><span style="background:#4fd1c5;color:#08110f;font-weight:700;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">CONTEXT RUNTIME</span><span style="background:#2f2f33;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">DEMO</span><span style="color:#9b99a1">This demo app is plugged into <b style="color:#e4e2e6">Context Runtime</b>, which optimizes which alert sources to pull per incident — correct verdict vs cost (0.900 vs 0.800); tool-using + approval-gated. <a href="https://github.com/redevops-io/context-runtime" style="color:#4fd1c5;text-decoration:none">learn more \u2192</a></span></div>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    import re as _cr_re
    page = render(fetch_activity())
    if _SENTINEL_CONSOLE is not None:
        panel = "<section class='shell' style='margin-top:var(--sp-4)'>" + _SENTINEL_CONSOLE.panel_html("sentinel") + "</section>"
        page = page.replace("<footer", panel + "<footer", 1)
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

    if action == "block_ip":
        return JSONResponse(_block_ip(body or {}))
    if action == "approve_block":
        return JSONResponse(_approve_block(body or {}))
    if action == "triage":
        return JSONResponse(_triage(body or {}))
    return JSONResponse(
        {"status": "error", "error": f"unknown action '{action}'",
         "supported": ["block_ip", "approve_block", "triage"]},
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
except Exception:  # noqa: BLE001 — agentic_os absent → no operator surface
    pass
