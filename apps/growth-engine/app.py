"""agentic-growth-engine — agent layer + GA4/HubSpot-style dashboard over a real Umami core.

Wraps the running self-hosted Umami instance (the OSS web-analytics core) with:

  * an agent layer that reads REAL Umami data over the REST API (stats + UTM/referrer
    metrics) and turns it into lead-source attribution + growth KPIs, and
  * an MD3 dashboard (same design tokens as deploy/module_service.py) rendered from that
    live data — no mock data.

Same pattern as the agentic-billing reference (Lago):
  1. point UMAMI_URL / WEBSITE_ID at the running Umami core (seed.py writes the .env),
  2. fetch_activity() pulls real records + computes growth KPIs,
  3. reuse BASE_CSS + the growth-engine render helpers (KPI tiles, lead-source bar
     breakdown, conversion-funnel bars, channel table with spend/CPL/ROAS),
  4. /agent/run actions are deterministic, with a human-approval gate on anything that
     moves ad budget (budget_change) — ad spend lives in external Ads platforms anyway.

Endpoints:
  GET  /health        -> {"status","core":"umami","connected": <bool from /api/heartbeat>}
  GET  /api/activity  -> live growth KPIs + lead-source attribution + funnel + channel table
  GET  /              -> MD3 growth dashboard rendered from the live Umami data
  POST /agent/run     -> {"action":"analyze"|"reallocate_budget"}

Config (env; seed.py writes agents/growth-engine/.env automatically):
  UMAMI_URL          REST base, default http://localhost:3002
  WEBSITE_ID         the Meridian Wealth website id (captured by seed.py)
  UMAMI_ADMIN_USER   login user, default admin
  UMAMI_ADMIN_PASS   login pass, default umami
  UMAMI_FRONT_URL    Umami UI link for the "Open in Umami" button
  PORT               uvicorn port, default 8205
  REDEVOPS_LLM_BASE_URL  OPTIONAL in-boundary model; if set (and actually in-boundary) /agent/run "analyze"
                     adds an LLM blurb via the governed router. Works fully without it.
"""
from __future__ import annotations

import html
import os

# ── enterprise permissions plane — gate tool/data access by (app, user) ──
# No-op without the context_runtime_enterprise package or CR_PERMISSIONS=1 (open-core default).
try:
    from context_runtime_enterprise.apps import bootstrap as _cr_bootstrap
    _req_principal = _cr_bootstrap("growth-engine")
except Exception:  # noqa: BLE001
    def _req_principal(request=None):
        return None
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# --- config ------------------------------------------------------------------
# The Umami REST client, KPIs and agentic actions live in core.py (pure — no web /
# context-runtime deps) so the Mission Runtime operator can invoke them and they can be
# tested against a fake Umami. Import config + core helpers from there; core loads .env.
from . import core
from .core import (  # noqa: E402
    UMAMI_URL, WEBSITE_ID, ADMIN_USER, ADMIN_PASS, UMAMI_FRONT_URL, TENANT, SUBTITLE,
    LEAD_VALUE, CHANNEL_CPC, CHANNEL_LABELS,
    _token, _headers, umami_connected, _range_ms, _get_stats, _get_metric,
    _channel_label, fetch_activity,
)

PORT = int(os.environ.get("PORT", "8205"))

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


app = FastAPI(title="agentic-growth-engine (Meridian Wealth Management · core: Umami)")


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


def _kpi_tiles(kpis: list[dict]) -> str:
    cells = ""
    for k in kpis:
        note = k.get("note", "")
        cls = "tile__delta"
        if note.startswith("+") or "↓ from" in note or "↑" in note:
            cls += " tile__delta--up"
        elif note.startswith("-") or "↓" in note:
            cls += " tile__delta--down"
        cells += (
            "<div class='tile'>"
            f"<div class='tile__label'>{_esc(k['label'])}</div>"
            f"<div class='tile__value'>{_esc(k['value'])}</div>"
            f"<div class='{cls}'>{_esc(note)}</div>"
            "</div>"
        )
    return f"<section class='kpi-row'>{cells}</section>"


def _barlist(title: str, items: list[dict], show_value: bool = False) -> str:
    rows = ""
    for b in items:
        pct = int(b["pct"])
        right = _esc(b.get("value", f"{pct}%")) if show_value else f"{pct}%"
        rows += (
            "<div class='barlist__row'>"
            f"<div class='barlist__label'>{_esc(b['label'])}</div>"
            f"<div class='bar'><span style='width:{pct}%'></span></div>"
            f"<div class='barlist__pct'>{right}</div>"
            "</div>"
        )
    return (
        "<div class='card'>"
        f"<div class='card__head'><h2 class='card__title'>{_esc(title)}</h2></div>"
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
            if i > 0 and any(ch.isdigit() for ch in txt) or txt in ("∞", "$0"):
                cells += f"<td class='num'>{_esc(txt)}</td>"
            else:
                cells += f"<td>{_esc(c)}</td>"
        body += f"<tr>{cells}</tr>"
    return (
        "<div class='card'>"
        f"<div class='card__head'><h2 class='card__title'>{_esc(table['title'])}</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>traffic: live from Umami</span>"
        + ("<span class='pill pill--warn'><span class='pill__dot'></span>spend/CPL/ROAS: modeled_estimate</span>"
           if table.get("basis") == "modeled_estimate" else "") + "</div>"
        + (f"<p class='body-s'>{_esc(table['note'])}</p>" if table.get("note") else "") +
        f"<table class='table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        "</div>"
    )


def _two_col(a: str, b: str) -> str:
    return (
        "<div class='grid'>"
        f"<div class='col-6'>{a}</div><div class='col-6'>{b}</div>"
        "</div>"
    )


def _section(label: str, body: str) -> str:
    return (
        "<section class='shell' style='gap:var(--sp-4)'>"
        f"<div class='section-label'>{_esc(label)}</div>{body}</section>"
    )


def _approval_banner(data: dict) -> str:
    """Surface the budget_change opportunity the agent can stage (approval-gated)."""
    channels = data.get("channels", [])
    paid = [c for c in channels if c.get("paid")]
    if len(paid) < 2:
        return ""
    # Recommend shifting from the worst-ROAS paid channel to the best.
    rated = [c for c in paid if c.get("roas") is not None]
    if len(rated) < 2:
        return ""
    best = max(rated, key=lambda c: c["roas"])
    worst = min(rated, key=lambda c: c["roas"])
    if best["label"] == worst["label"]:
        return ""
    return (
        "<div class='banner'>"
        "<span class='pill pill--warn'><span class='pill__dot'></span>1 approval</span>"
        "<span class='label-m' style='text-transform:uppercase;color:var(--warning)'>budget_change</span>"
        f"<span class='body-m'>Shift spend from {_esc(worst['label'])} "
        f"(modeled ROAS {worst['roas']:.1f}x) to {_esc(best['label'])} (modeled ROAS {best['roas']:.1f}x; "
        "modeled_estimate, illustrative until an ad connector is configured) — "
        f"{_esc(best['label'])} converts cheaper this month. Agent stages the change; "
        "ad spend lives in the external Ads platform, so a human approves before it moves.</span>"
        "</div>"
    )


def render(data: dict) -> str:
    connected = data["connected"]
    conn_txt = "core: Umami connected" if connected else "core: Umami UNREACHABLE"
    conn_cls = "pill--success" if connected else "pill--danger"
    status_pill = (
        f"<span class='pill {conn_cls}'><span class='pill__dot'></span>agent active · {_esc(conn_txt)}</span>"
    )
    live_badge = "<span class='pill pill--info'><span class='pill__dot'></span>data: live from Umami</span>"
    open_btn = (f"<a class='btn' href='{_esc(data['front_url'])}' target='_blank' "
                "rel='noopener'>Open in Umami ↗</a>")

    funnel = data["funnel"]
    mid = _two_col(
        _barlist(funnel["title"], funnel["items"], show_value=True),
        _barlist(data["bars"]["title"], data["bars"]["items"]),
    )
    detail = _table_card(data["table"])

    body = (
        _approval_banner(data)
        + _kpi_tiles(data["kpis"])
        + _section("Funnel & channels", mid)
        + _section("Lead-source attribution", detail)
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Growth Engine — {_esc(TENANT)}</title>
{FONT_LINK}
<style>{BASE_CSS}{PAGE_CSS}</style>
</head>
<body class="page">
<div class="shell">
  <header class="appbar">
    <div class="appbar__row">
      <h1>Growth Engine</h1>
      {status_pill}
      {live_badge}
      <span class="spacer"></span>
      {open_btn}
    </div>
    <div class="appbar__tenant"><b>{_esc(TENANT)}</b> · core: Umami (open-source web analytics)</div>
    <div class="appbar__sub">{_esc(SUBTITLE)}</div>
  </header>
  {body}
  <footer class="footer">agentic-growth-engine · live attribution for {_esc(TENANT)} ·
    <a href="/api/activity">/api/activity</a> · agent + human, on a real Umami core · redevops.io Agentic Business OS</footer>
</div>
</body>
</html>"""


# --- governed LLM (N7): every model call routes through agentic_os.app.llm.governed_text ----------------
# Fail-closed: with no in-boundary endpoint (REDEVOPS_LLM_BASE_URL unset / not actually in-boundary) or a
# router refusal governed_text returns None and the caller degrades to deterministic output. There is no
# direct-HTTP and no external-provider fallback.
def _llm_blurb(prompt: str) -> str | None:
    """A short reasoning blurb from the GOVERNED in-boundary model, or None (deterministic output stands)."""
    try:
        from agentic_os.app.llm import governed_text
        from agentic_os.governance.classification import DataClassification
    except Exception:  # noqa: BLE001
        return None
    return governed_text(prompt, classifications=(DataClassification.CUSTOMER_CONFIDENTIAL,),
                         max_tokens=220, temperature=0.3)


class _GovernedConsoleModel:
    """The conversational console's model object: every ``complete`` goes through ``governed_text`` with an
    explicit classification. Raises when there is no compliant route so the console drops to its
    deterministic keyword/primer fallbacks (it never reaches a model on its own)."""

    def complete(self, req):
        from types import SimpleNamespace
        from agentic_os.app.llm import governed_text
        from agentic_os.governance.classification import DataClassification
        prompt = "\n\n".join(str(m.get("content", "")) for m in req.messages)
        txt = governed_text(prompt, classifications=(DataClassification.CUSTOMER_CONFIDENTIAL,), system=req.system or "",
                            max_tokens=int(req.max_tokens or 600), temperature=0.2)
        if not txt:
            raise RuntimeError("no compliant in-boundary model route")
        return SimpleNamespace(text=txt, model="in-boundary", tier="in_boundary", prompt_tokens=0,
                               completion_tokens=0, est_cost_usd=0.0, cache_hit=False, models_used=())

    def info(self):
        from types import SimpleNamespace
        return SimpleNamespace(name="governed-in-boundary")


# --- agentic actions (thin wrappers over core; core stays context-runtime-free) ---
def _analyze() -> dict:
    """Channel attribution over the real Umami core, with the optional LLM narration blurb."""
    return core.analyze(blurb=_llm_blurb)


def _reallocate_budget(body: dict) -> dict:
    """Stage an ad-budget change for human approval (never auto-executed)."""
    return core.reallocate_budget(body)


# ──────── conversational agent · Context Runtime (LLM) + redevops-rag (grounding) + agent-harness (tools) ────────
try:
    from context_runtime.integrations.agent_console import AgentConsole as _AgentConsole, tool as _crtool
except Exception:  # noqa: BLE001 — context-runtime absent → no console
    _AgentConsole = None

_GROWTH_PRIMER = """A PAGEVIEW is one load of a page. A VISITOR is one unique person (deduplicated by device), so 200 pageviews from 50 people = 200 pageviews / 50 visitors. A SESSION (Umami calls it a "visit") is one continuous browsing burst by a visitor; it ends after ~30 min of inactivity, so one visitor can have several sessions over a month.

BOUNCE RATE is the share of sessions where the visitor viewed only one page and then left without interacting. A high bounce rate isn't automatically bad — a landing page that answers a "call us" search does its job in one page. As a rough guide: under ~40% is strong, 40–60% is normal, and over ~70% on pages meant to convert is worth investigating (slow load, wrong audience, or a mismatch between the ad and the page).

A REFERRER is the site a visitor came from (the previous URL). Traffic SOURCES / CHANNELS group referrers into buckets: ORGANIC (search engines like Google), DIRECT (typed the URL or no referrer), REFERRAL (a link on another site), SOCIAL (Facebook, Instagram), and PAID (ad clicks). Knowing your channel mix tells you where to spend and where you're exposed.

UTM PARAMETERS are tags you add to a link — utm_source (e.g. google), utm_medium (e.g. cpc, email), utm_campaign (e.g. retirement-planning) — so Umami can attribute a visit to a specific campaign. Always tag ad and email links with UTMs; otherwise that traffic lands in "direct" and you can't tell what worked.

An EVENT is a custom action you track beyond a pageview — a "Schedule a consultation" button click, a form submit, a phone tap. Events are how you measure CONVERSIONS (the actions that make money), not just traffic. In Umami you fire an event with data-umami-event on the element or track() in code.

To READ THE DASHBOARD: start with visitors and pageviews for the volume, check bounce rate for engagement quality, then open referrers / UTM sources to see which channels drive the visits. "Why is my bounce rate high?" usually means traffic from the wrong audience or a slow/irrelevant landing page. "Where is my traffic coming from?" is answered by the top referrers and UTM sources — that's your channel mix."""


def _bounce_rate(totals: dict) -> str:
    visits = int(totals.get("visits", 0) or 0)
    bounces = int(totals.get("bounces", 0) or 0)
    return f"{round(100 * bounces / visits)}%" if visits else "n/a"


def _t_traffic_summary(_a: dict) -> dict:
    d = fetch_activity()
    t = d["totals"]
    br = _bounce_rate(t)
    return {"text": f"Last 30 days: {t['pageviews']} pageviews from {t['visitors']} visitors "
            f"across {t['visits']} sessions, bounce rate {br}. "
            f"Top traffic source by volume: {t['top_channel']}.",
            "data": {"pageviews": t["pageviews"], "visitors": t["visitors"],
                     "sessions": t["visits"], "bounce_rate": br, "top_channel": t["top_channel"]}}


def _t_top_pages(_a: dict) -> dict:
    start, end = _range_ms(30)
    rows = _get_metric("url", start, end)
    if not rows:
        return {"text": "No page data available right now (Umami may be unreachable).", "data": []}
    top = sorted(rows, key=lambda r: int(r.get("y", 0)), reverse=True)[:10]
    lines = [f"{r.get('x') or '/'} — {int(r.get('y', 0))} views" for r in top]
    return {"text": "Most-visited pages (30d):\n" + "\n".join(lines), "data": top}


def _t_top_referrers(_a: dict) -> dict:
    d = fetch_activity()
    refs = d.get("utm", {}).get("referrers", [])
    if not refs:
        return {"text": "No referrer data yet — most traffic is likely direct, or Umami is unreachable.",
                "data": []}
    top = sorted(refs, key=lambda r: int(r.get("y", 0)), reverse=True)[:10]
    lines = [f"{r.get('x') or '(direct)'} — {int(r.get('y', 0))} sessions" for r in top]
    return {"text": "Where your traffic comes from (30d):\n" + "\n".join(lines), "data": top}


if _AgentConsole is not None:
    _GROWTH_CONSOLE = _AgentConsole(
        f"{TENANT} Growth", _GROWTH_PRIMER,
        tools=[
            _crtool("traffic_summary", "current traffic KPIs — pageviews, visitors, sessions, bounce rate, top source", _t_traffic_summary),
            _crtool("top_pages", "the most-visited pages over the last 30 days", _t_top_pages),
            _crtool("top_referrers", "where traffic comes from — top referrers / sources over the last 30 days", _t_top_referrers),
        ],
        suggestions=["How's my traffic?", "Top pages this week", "Where's my traffic from?", "What is bounce rate?"],
        subtitle="Ask how to read your analytics — or have the agent show your traffic, pages and sources.",
        model=_GovernedConsoleModel(),
        allow_side_effects=[],  # all tools are read-only
    )
else:
    _GROWTH_CONSOLE = None


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "core": "umami", "connected": umami_connected()}


@app.post("/api/agent")
async def api_agent(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        body = {}
    if _GROWTH_CONSOLE is None:
        return JSONResponse({"intent": "help", "text": "The assistant is offline (context-runtime unavailable).", "evidence": []})
    return JSONResponse(_GROWTH_CONSOLE.respond((body or {}).get("message", ""), principal=_req_principal(request)))


@app.get("/api/activity")
def activity() -> JSONResponse:
    return JSONResponse(fetch_activity())


_CR_BANNER = """<div style="position:sticky;top:0;z-index:9998;background:linear-gradient(90deg,#10201d,#17171a);border-bottom:1px solid #2f2f33;color:#e4e2e6;font:13px/1.4 Roboto,system-ui,sans-serif;padding:9px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap"><span style="background:#4fd1c5;color:#08110f;font-weight:700;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">CONTEXT RUNTIME</span><span style="background:#2f2f33;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">DEMO</span><span style="color:#9b99a1">This demo app is plugged into <b style="color:#e4e2e6">Context Runtime</b>, which optimizes which attribution sources to use — accuracy vs cost. <a href="https://github.com/redevops-io/context-runtime" style="color:#4fd1c5;text-decoration:none">learn more \u2192</a></span></div>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    import re as _cr_re
    page = render(fetch_activity())
    if _GROWTH_CONSOLE is not None:
        panel = "<section class='shell' style='margin-top:var(--sp-4)'>" + _GROWTH_CONSOLE.panel_html("growth") + "</section>"
        page = page.replace("<footer", panel + "<footer", 1)
    page = _cr_re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + _CR_BANNER, page, count=1)
    if "_CR_BANNER" not in page:  # no <body> matched → prepend
        page = _CR_BANNER + page
    return page


@app.post("/agent/run")
async def agent_run(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:
        body = {}
    action = (body or {}).get("action", "")

    if action == "analyze":
        return JSONResponse(_analyze())
    if action == "reallocate_budget":
        return JSONResponse(_reallocate_budget(body or {}))
    return JSONResponse(
        {"status": "error", "error": f"unknown action '{action}'",
         "supported": ["analyze", "reallocate_budget"]},
        status_code=400,
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive growth as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_growth_operator
    app.include_router(build_growth_operator().router())
    # Runtime-native: register this app's governed contract at boot (guarded with the mount above).
    from agentic_os.app_kit.boot import register_app_manifest as _register_runtime_native
    from . import manifest as _runtime_native_manifest
    _register_runtime_native(_runtime_native_manifest)
except Exception as _op_exc:  # agentic_os absent / operator build failed — surface it, do not hide it
    import logging as _logging
    _logging.getLogger("agentic_os.app").error(
        "%s: operator/capability surface NOT mounted (%s: %s) — /invoke + /capabilities are unavailable",
        __name__, type(_op_exc).__name__, _op_exc)
