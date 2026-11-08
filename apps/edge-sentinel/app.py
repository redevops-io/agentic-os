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
  SENTINEL_BIND_HOST  interface to bind, default 127.0.0.1 (loopback). A reachable bind also needs
                      SENTINEL_ALLOW_EXPOSURE=1, and either an operator credential or
                      SENTINEL_ALLOW_UNAUTHENTICATED_EXPOSURE=1 — see security.py.
  SENTINEL_BASIC_AUTH_USER / _PASS  operator credentials for execution (Basic auth; fail-closed 503 unset)
  SENTINEL_BLOCK_ENABLED            default OFF: an authenticated execution is a dry run without it
  WAZUH_TLS_VERIFY    default on; `off` skips certificate verification, `/<path>` uses a private CA bundle
  REDEVOPS_LLM_BASE_URL  OPTIONAL in-boundary model; if set (and actually in-boundary) `triage` adds an LLM
                      blurb via the governed router. The endpoint works fully without it.
  EDGE_SENTINEL_DEMO  opt-in fixture core for demos and UI acceptance: reads come from synthetic RFC 5737
                      data and every CrowdSec side effect is refused at core. It grants no exposure and no
                      exemption from the rules above — see fixture_mode.py.
"""
from __future__ import annotations

import html
import json
import os

# ── enterprise permissions plane — gate tool/data access by (app, user) ──
# Basic default without the enterprise package and without CR_PERMISSIONS / CR_IDENTITY_*; if either is set and the gate cannot
# be installed, agentic_os.app_gate refuses to start the app (it never runs ungated by accident).
from agentic_os.app_gate import RequestContext as _RequestContext, denial_result as _denial_result, deny_response as _deny_response, guarded as _guarded, install_gate as _install_gate
from agentic_os.app_gate import with_authorization as _with_authorization
_GATE_APP = "edge-sentinel"
_req_principal = _install_gate(_GATE_APP)   # starts gated or not at all when permissions were requested (RR-01)
import re
import subprocess
import time
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

# --- config ------------------------------------------------------------------
# The CrowdSec LAPI/cscli clients, KPIs and agentic actions live in core.py (pure — no web /
# context-runtime deps) so the Mission Runtime operator can invoke them and they can be
# tested against a fake CrowdSec. Import config + core helpers from there; core loads .env.
from . import core
from . import live  # noqa: E402 — wires the SOC app to the Security Intelligence Core (real cases)
from . import ratelimit  # noqa: E402 — per-IP token bucket on the unauthenticated POST routes
from . import ipguard  # noqa: E402 — ban-target validation
from . import auth  # noqa: E402 — the HTTP-Basic operator gate for real execution
# Self-contained security planes — each degrades gracefully + ships labelled SAMPLE until its token
# is set (netbird/portabase) or reports unavailable until wired (deploy_scan). Their panels reuse the
# page chrome below via a deferred import, so there is no import cycle.
from . import netbird  # noqa: E402 — NetBird zero-trust access review
from . import portabase  # noqa: E402 — Portabase backup/continuity coverage
from . import deploy_scan  # noqa: E402 — runtime-DAST proxy to the deploy-scan MCP service
from . import permissions_ui  # noqa: E402 — live vuln-DB permissions demo
from . import security as _security  # noqa: E402 — secure defaults: loopback bind, TLS verify, headers (P9)
from . import fixture_mode  # noqa: E402 — opt-in fixture core for demos and UI acceptance (P4)
from .core import (  # noqa: E402
    TENANT, SUBTITLE, fetch_activity, _severity,
)
# `crowdsec_connected` is deliberately NOT imported by name: `core.crowdsec_connected()` is called where it
# matters (see /health), and a bare alias taken at module load would keep pointing at the real LAPI probe
# after fixture_mode swaps the core — a live reachability check inside a deployment that says it is synthetic.

# Installed before the app object exists, so no request can be served against a half-swapped core. The
# P9 exposure guard reads its own variables and runs regardless: demo mode is about where data comes
# from, never about who may reach the app.
_FIXTURE_REPORT = fixture_mode.install_if_enabled()

PORT = int(os.environ.get("PORT", "8203"))
# What the app listens on when run by `python -m edge-sentinel.app` (P9). A container sets
# SENTINEL_BIND_HOST explicitly, because a container is reachable by design; a laptop gets loopback.
BIND_HOST = _security.resolve_bind_host()

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


app = FastAPI(title="edge-sentinel (Meridian Wealth Management · core: CrowdSec)")


@app.exception_handler(fixture_mode.DemoRefused)
async def _demo_refused(request, exc: fixture_mode.DemoRefused):
    """A fixture-mode refusal is a policy decision, not a server fault: 403 with the reason, and the same
    shape every other refusal here uses. It exists because the refusal is raised from `core`, well below
    the route that triggered it — no handler has to remember to check the mode."""
    return JSONResponse(status_code=403, content={"ok": False, "refused": "demo_mode",
                                                  "reason": str(exc),
                                                  "synthetic": True})


from agentic_os.app_kit import route_authz as _route_authz  # noqa: E402
from .route_policy import APP as _APP, ROUTE_POLICIES as _ROUTE_POLICIES  # noqa: E402
_route_authz.install(app, _APP, _ROUTE_POLICIES)   # one enforcement layer for every write route
app.add_middleware(_RequestContext)   # the active gate (basic or enterprise) sees THIS request's principal in every route


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Every response carries the same baseline headers (P9). setdefault, so a route that states its own
    policy — a download, an embed — keeps its own rather than being overwritten by ours."""
    response = await call_next(request)
    for _h, _v in _security.SECURITY_HEADERS.items():
        response.headers.setdefault(_h, _v)
    return response


@app.on_event("startup")
def _check_declared_bind():
    """A process started by someone else's `uvicorn --host` cannot be stopped from here, so the container
    image declares its bind host in the environment (SENTINEL_BIND_HOST) and this checks the declaration
    before it serves a request: a reachable port with no credentials and no explicit opt-out raises
    ExposureRefused, which fails the uvicorn startup outright — the refusal is confirmed by the test that
    enters the app's lifespan. Unset means the guard has nothing to check — the `python -m
    edge-sentinel.app` path guards itself and prints the refusal as a startup error."""
    _declared = os.environ.get("SENTINEL_BIND_HOST", "").strip()
    if _declared:
        _security.exposure_guard(_declared)


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
  --font-sans:system-ui,-apple-system,"Segoe UI","Roboto",sans-serif;
  --font-mono:ui-monospace,"SF Mono","Roboto Mono",monospace;
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
.status-banner--quiet{border-left-color:var(--info);background:var(--info-container)}
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

# No third-party font request (P9): a page view used to fetch Roboto from fonts.googleapis.com and
# fonts.gstatic.com, which leaks every view of a SOC dashboard to Google, breaks an offline or air-gapped
# host, and contradicts a content policy of 'self'. Roboto is still first-choice-by-name in the CSS above
# when the machine has it installed; otherwise the system UI/mono stack renders. FONT_LINK stays a name
# the page templates interpolate so this decision has one home and a missing-font page is impossible.
FONT_LINK = ""


def _esc(v) -> str:
    return html.escape(str(v))


def _src(data: dict, key: str) -> tuple[bool, str]:
    """(ok, note) for one CrowdSec source ('decisions' or 'alerts'); a payload without a status counts as read."""
    st = data.get(f"{key}_status")
    if not isinstance(st, dict):
        return True, ""
    return bool(st.get("ok", True)), str(st.get("note", ""))


def _read_note(data: dict) -> str:
    """What was read and when, per source, or why there is no reading. Never claims a read for a source that failed."""
    if not data.get("connected"):
        return "no reading: CrowdSec unreachable"
    d_ok, d_note = _src(data, "decisions")
    a_ok, a_note = _src(data, "alerts")
    at = data.get("read_at") or "an unknown time"
    if d_ok and a_ok:
        return f"read from CrowdSec at {at}"
    parts = []
    parts.append(f"decisions read at {data.get('decisions_read_at') or at}" if d_ok else f"decisions not read ({d_note})")
    parts.append(f"alerts read at {data.get('alerts_read_at') or at}" if a_ok else f"alerts not read ({a_note})")
    return "; ".join(parts)


def _alerts_note(data: dict) -> str:
    """The alert feed card's own read note (it can fail while decisions were read)."""
    if not data.get("connected"):
        return "no reading: CrowdSec unreachable"
    a_ok, a_note = _src(data, "alerts")
    if not a_ok:
        return f"alerts not read ({a_note})"
    return f"read from CrowdSec at {data.get('alerts_read_at') or data.get('read_at') or 'an unknown time'}"


def _origin_tag(origin: str, scenario_full: str = "") -> str:
    """Visible tag for an entry that was added by hand. It says only what is known: a SAMPLE HISTORY marker in the reason
    (written by seed.py) is shown as such; an origin of cscli is 'manual', which can be sample history or an approved block."""
    if str(scenario_full or "").startswith("SAMPLE HISTORY:"):
        return (" <span class='pill pill--neutral' title='Marked SAMPLE HISTORY when it was added (seed.py). Not a detection by a "
                "CrowdSec scenario.'>sample history</span>")
    if origin != "cscli":
        return ""
    return (" <span class='pill pill--neutral' title='Added manually via cscli. This can be demo sample history or an approved "
            "block; it was not detected by a CrowdSec scenario.'>manual (cscli)</span>")


_SAMPLE_STATE: dict = {"netbird": {"ts": 0.0, "sample": True, "busy": False}, "portabase": {"ts": 0.0, "sample": True, "busy": False}}
_SAMPLE_TTL = 30.0


def _refresh_sample_state(name: str) -> None:
    """Re-read whether a service panel is showing sample data, from the same snapshot() the panel uses (so a token set
    against an unreachable service is reported as sample, exactly like the panel)."""
    mod = netbird if name == "netbird" else portabase
    try:
        sample = not mod.snapshot().get("live")
    except Exception:  # noqa: BLE001
        sample = True
    _SAMPLE_STATE[name].update(ts=time.monotonic(), sample=sample, busy=False)


def _is_sample(name: str) -> bool:
    """True when the service's panel is (as far as is known) showing sample data. With no token configured the answer is
    immediate; with a token the snapshot is refreshed in the background and 'sample' is assumed until it says otherwise."""
    st = _SAMPLE_STATE[name]
    token = netbird.NETBIRD_API_TOKEN if name == "netbird" else portabase.PORTABASE_API_TOKEN
    if time.monotonic() - st["ts"] < _SAMPLE_TTL and st["ts"]:
        return st["sample"]
    if not token:
        _refresh_sample_state(name)
        return _SAMPLE_STATE[name]["sample"]
    if not st["busy"]:
        st["busy"] = True
        import threading as _t
        _t.Thread(target=_refresh_sample_state, args=(name,), daemon=True, name=f"sample-state-{name}").start()
    return st["sample"]


def _chips() -> list[str]:
    return ["Any threats today?", "List the active bans",
            "Review network access" + (" (sample data)" if _is_sample("netbird") else ""),
            "Which vulnerability scanners are installed?"]


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
    """Red when CrowdSec is unreachable or holds active decisions; otherwise a neutral statement of what was read and when."""
    if not data["connected"]:
        return (
            "<div class='status-banner status-banner--threat'>"
            "<span class='status-banner__icon'>!</span>"
            "<span class='pill pill--danger'><span class='pill__dot'></span>CORE UNREACHABLE</span>"
            "<span class='body-m'>CrowdSec LAPI is not responding — detections cannot be read.</span>"
            "</div>"
        )
    d_ok, d_note = _src(data, "decisions")
    a_ok, a_note = _src(data, "alerts")
    notes = []
    if not d_ok:
        notes.append(f"the decisions could not be read ({d_note})")
    if not a_ok:
        notes.append(f"the alert feed could not be read ({a_note})")
    unread = (" The count for " + " and ".join(n for n in ("decisions" if not d_ok else "", "alerts" if not a_ok else "") if n) +
              " is not a reading.") if notes else ""
    if data["has_threat"] and d_ok:
        crit = [a for a in data["alerts"] if a["severity"] == "critical"]
        n = len(data["decisions"])
        top = data["alerts"][0]["scenario"] if data["alerts"] else "—"
        alert_part = (f"{len(crit)} critical alert(s) by scenario name. Latest alert: <span class='mono'>{_esc(top)}</span>. "
                      if a_ok else "")
        feed = f" CrowdSec: {_esc('; '.join(notes))}.{unread}" if notes else ""
        return (
            "<div class='status-banner status-banner--threat'>"
            "<span class='status-banner__icon'>&#9888;</span>"
            f"<span class='pill pill--danger'><span class='pill__dot'></span>ACTIVE DECISIONS · {n}</span>"
            f"<span class='body-m'>CrowdSec holds {n} active decision(s). {alert_part}{_esc(_read_note(data))}.{feed} "
            "Entries tagged manual (cscli) or sample history were added by hand and may be sample data. Sensitive blocks are human-approved.</span>"
            "</div>"
        )
    if notes:
        label = "DECISIONS NOT READ" if not d_ok else "ALERT FEED UNAVAILABLE"
        return (
            "<div class='status-banner status-banner--quiet'>"
            "<span class='status-banner__icon'>&#9888;</span>"
            f"<span class='pill pill--warn'><span class='pill__dot'></span>{label}</span>"
            f"<span class='body-m'>CrowdSec: {_esc('; '.join(notes))}.{unread} Nothing can be said about the unread part; "
            f"{_esc(_read_note(data))}.</span>"
            "</div>"
        )
    return (
        "<div class='status-banner status-banner--quiet'>"
        "<span class='status-banner__icon'>&#9432;</span>"
        "<span class='pill pill--info'><span class='pill__dot'></span>Quiet reading</span>"
        f"<span class='body-m'>CrowdSec: {len(data['decisions'])} active decisions and {len(data['alerts'])} alerts, "
        f"{_esc(_read_note(data))}. This is what CrowdSec currently holds. It is not an assessment of the "
        "environment: it does not show that nothing happened or that any sensor is watching.</span>"
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
                f"<div class='feed__scenario'>{_esc(a['scenario'])}{_origin_tag(a.get('origin', ''), a.get('scenario_full', ''))}</div>"
                f"<div class='feed__meta'>from <span class='feed__src'>{_esc(a['source'])}</span> "
                f"· {_esc(a['scope'])} · {_esc(a['events'])} event(s)</div>"
                "</div>"
                f"<div class='feed__time'>{_esc(a['ago'])}</div>"
                "</div>"
            )
    if not rows:
        rows = ("<div class='feed__row'><div class='feed__meta'>" + ("No alerts in the CrowdSec store." if _src(data, "alerts")[0] else f"Alerts not read ({_esc(_src(data, 'alerts')[1])}).") + "</div></div>")
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Alert feed · by severity</h2>"
        f"<span class='pill pill--info'><span class='pill__dot'></span>{_esc(_alerts_note(data))}</span></div>"
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
        body = ("<div class='barlist__label'>No scenarios recorded.</div>" if _src(data, "alerts")[0] else "<div class='barlist__label'>Alerts not read, so no scenarios are shown.</div>")
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Alerts by scenario (from the CrowdSec reading)</h2></div>"
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
            f"<td class='mono'>{_esc(d['scenario'])}{_origin_tag(d.get('origin', ''), d.get('scenario_full', ''))}</td>"
            f"<td>{_sev_pill(d['severity'])}</td>"
            f"<td class='mono'>{_esc(d['duration'])}</td>"
            f"<td><span class='pill pill--danger'>{_esc(d['type'])}</span></td>"
            "</tr>"
        )
    if not rows:
        rows = ("<tr><td colspan='6'>No active decisions.</td></tr>" if _src(data, "decisions")[0] else f"<tr><td colspan='6'>Decisions not read ({_esc(_src(data, 'decisions')[1])}).</td></tr>")
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Attack sources · active decisions</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>GET /v1/decisions</span></div>"
        "<table class='table'><thead><tr>"
        "<th>Source</th><th>Scope</th><th>Scenario</th><th>Severity</th><th>Expires in</th><th>Action</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "<div class='body-s' style='color:#9b99a1'>Entries tagged manual (cscli) were added by hand with cscli: demo sample "
        "history or an approved block. Entries tagged sample history were marked as such when they were added. Neither is a "
        "detection by a CrowdSec scenario.</div>"
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
        return "<span style='background:#0d1f1c;color:#4fd1c5;border:1px solid #4fd1c5;border-radius:5px;padding:2px 9px;font-size:11px;font-weight:700'>READ FROM THE SERVICE</span>"
    return f"<span style='background:#241f14;color:#e0b000;border:1px solid #6b5a1e;border-radius:5px;padding:2px 9px;font-size:11px;font-weight:700'>SAMPLE DATA — {_esc(hint)}</span>"


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
    conn_txt = "CrowdSec LAPI reachable" if connected else "CrowdSec LAPI UNREACHABLE"
    conn_cls = "pill--success" if connected else "pill--danger"
    status_pill = (
        f"<span class='pill {conn_cls}'><span class='pill__dot'></span>{_esc(conn_txt)}</span>"
    )
    live_badge = f"<span class='pill pill--info'><span class='pill__dot'></span>{_esc(_read_note(data))}</span>"
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
  <footer class="footer">edge-sentinel · CrowdSec readings for the {_esc(TENANT)} demo tenant ·
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
        elif act and act.get("state") in ("APPROVED", "FAILED"):
            rid = act["request_id"]
            failed = act.get("state") == "FAILED"
            action = (
                "<span class='pill pill--warn'><span class='pill__dot'></span>"
                f"{'last attempt failed · retry' if failed else 'approved · awaiting operator'}</span> "
                f"<button class='btn' onclick=\"esExecute('{_esc(s['case_id'])}','{_esc(rid)}')\">"
                f"{'Retry' if failed else 'Execute'} (operator) &#128274;</button>"
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
                "CrowdSec alert read (none at the last read). Nothing here is canned.</td></tr>")
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Investigation cases · evidence workspace</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>GET /api/cases</span></div>"
        "<div class='body-m' style='color:#9b99a1;margin:-4px 0 10px'>Each row is a replayable "
        "<span class='mono'>SecurityCase</span> built from immutable evidence — raw alert &rarr; observation "
        "&rarr; finding &rarr; approval-gated action. <b>Approve is open</b> (it only stages the action); the "
        "<b>real kick-off is operator-authenticated</b> and needs an enable flag (SENTINEL_BLOCK_ENABLED); without it this execute "
        "step is a <b>dry run</b>: nothing is blocked, and the edge is never touched on approve or reject. Cases are rebuilt from the alerts "
        "CrowdSec currently holds, including hand-added (cscli) entries that may be sample history.</div>"
        "<table class='table'><thead><tr>"
        "<th>Severity</th><th>Case</th><th>Status</th><th>Evidence</th><th>Replay</th><th>Response</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "<script>\n"
        "async function esRespond(cid,rid,approve){\n"
        "  const r = await fetch(`/api/cases/${cid}/respond`,{method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({request_id:rid,approved:approve})});\n"
        "  const j = await r.json();\n"
        "  if(j.simulated){alert('Demo: simulated - nothing was recorded. Sign in as the operator to record a real decision.');}\n"
        "  else if(approve){alert('Approved and staged. Real execution needs an authenticated operator — "
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
        "  alert(j.dry_run ? 'Dry run: nothing was blocked (SENTINEL_BLOCK_ENABLED unset). Receipt: '+"
        "(j.receipt?j.receipt.status:'(none)') : 'Executed. Receipt: '+(j.receipt?j.receipt.status:'(none)')+"
        "' · verified='+(j.verification?j.verification.verified:'n/a'));\n"
        "  location.reload();\n"
        "}\n"
        "</script>"
        "</div>"
    )


# --- governed LLM (N7): every model call routes through agentic_os.app.llm.governed_text ----------------
# Fail-closed: with no in-boundary endpoint (REDEVOPS_LLM_BASE_URL unset / not actually in-boundary) or a
# router refusal governed_text returns None and the caller degrades to deterministic output. There is no
# direct-HTTP and no external-provider fallback.
def _llm_blurb(prompt: str) -> str | None:
    """A short reasoning blurb from the GOVERNED in-boundary model, or None (deterministic output stands)."""
    # Demo mode makes no model call. Returning None is this function's designed degradation — the triage
    # panel's deterministic text is what the demo is actually demonstrating — and it keeps the demo's claim
    # that nothing left the process true even on a host where an in-boundary model IS configured.
    if fixture_mode.read_refusal("model call"):
        return None
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
        # Same boundary as `_llm_blurb`, and it must be here too: this is a second, independent path to the
        # model, reached by the console's own turn loop. DemoRefused subclasses RuntimeError, so the console's
        # existing fallback still catches it if it chooses to; if it propagates, the registered handler answers
        # 403. Either way the demo makes no model call.
        _demo_read = fixture_mode.read_refusal("console model call")
        if _demo_read:
            raise fixture_mode.DemoRefused(_demo_read)
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


# --- agentic actions ---------------------------------------------------------
@_guarded(_GATE_APP, "ban_ip")
def _block_ip(body: dict) -> dict:
    """Blocking traffic is the SENSITIVE action — never auto-executed.

    Mirrors the module's `approval_required:[remediation]`: stage the block and return
    pending_approval. The actual ban only happens via the `approve_block` action.
    """
    target, _dur, _why, refusal = core.preflight_block(body or {})   # refuse un-banable targets at staging time too
    if refusal:
        return refusal
    ip = target.value
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


@_guarded(_GATE_APP, "ban_ip")
def _approve_block(body: dict) -> dict:
    """The approved path: enforce the ban decision on the real CrowdSec core — but only when
    SENTINEL_BLOCK_ENABLED is set. Default is dry-run, so an approval in the public demo never mutates the
    live edge by accident (the same safety flag the case-execute path honors).

    Thin wrapper over core.block_ip (POST /v1/decisions) — core stays context-runtime-free.
    """
    target, _dur, _why, refusal = core.preflight_block(body or {})   # validate first, so a refusal shows in dry-run too
    if refusal:
        return refusal
    ip = target.value
    if not auth.block_enabled():
        return {"status": "dry_run", "action": "approve_block", "ip": ip,
                "summary": f"Dry run: nothing was blocked. Would have blocked {ip}.",
                "detail": "Dry run: SENTINEL_BLOCK_ENABLED is not set, so nothing was blocked and the edge was not touched."}
    return core.block_ip(body or {})


def _unblock(body: dict) -> dict:
    """Compensating action (saga undo) for _approve_block — lift the ban."""
    return core.unblock_ip(body or {})


@_guarded(_GATE_APP, "sentinel.triage")
def _triage(body: dict) -> dict:
    """Summarize the current alerts/decisions over the real CrowdSec core, with the
    optional LLM narration blurb (core stays context-runtime-free)."""
    return core.triage(blurb=_llm_blurb)


# ──────── conversational agent · Context Runtime (LLM) + redevops-rag (grounding) + agent-harness (tools) ────────
try:
    from context_runtime.integrations.agent_console import AgentConsole as _AgentConsole, tool as _crtool
except Exception:  # noqa: BLE001 — context-runtime absent → no console
    _AgentConsole = None

_SENTINEL_PRIMER = """CrowdSec is the open-source engine watching your network. It reads your logs, spots attacks, and records ban decisions for attacking IP addresses; whether anything is actually blocked depends on a bouncer, which edge-sentinel neither provides nor checks. edge-sentinel is the agent layer on top: you can ask it what CrowdSec currently holds, and it keeps a human in the loop before any block is executed.

The LAPI (Local API) is CrowdSec's brain: it holds the list of who should be blocked and hands that list out. A BOUNCER is the muscle — the component that actually enforces a block (in a firewall, web server, or proxy) by asking the LAPI "should I let this IP in?". Detection and enforcement are separate on purpose.

A DECISION is an instruction to block a source, almost always a BAN on an IP address for a set DURATION (e.g. 4h). "Active decisions" = the bans CrowdSec currently holds; a bouncer is what enforces them, and edge-sentinel does not know whether one is present. When the duration runs out the decision expires. To ban an IP for real, the agent runs `cscli decisions add`; to lift one you run `cscli decisions delete --ip <address>` (this is how you UNBAN someone).

An ALERT is CrowdSec saying "I detected an attack." Read an alert by its SCENARIO (what the attacker did), its SOURCE (the IP it came from), and its event count (how many suspicious requests). One alert usually leads to one decision, but not always — some alerts are just logged for awareness.

A SCENARIO is the named pattern of bad behaviour that triggered the alert. http-bruteforce means someone hammered a login/form trying many passwords; ssh-bf (SSH brute-force) is the same against SSH logins; http-probing / http-path-traversal means someone poked around looking for weak spots; port scans map out open doors. Brute-force and exploit scenarios are the ones to take seriously.

A PARSER turns raw log lines into structured events; a COLLECTION is a ready-made bundle of parsers and scenarios for a given service (nginx, ssh, etc.) that you install so CrowdSec knows what to look for. You rarely touch these day to day — they're the detection recipes.

A WHITELIST tells CrowdSec to never block certain trusted IPs (your office, your VPN, a monitoring service). A FALSE POSITIVE is a legitimate visitor wrongly flagged — if a real customer or your own address gets blocked, unban them and add them to a whitelist so it doesn't recur. When unsure, prefer a short ban over a permanent one.

Should you worry? A handful of brute-force or probing alerts from random internet IPs is common background noise; say what was read, and do not say that anything was blocked unless the data shows an enforcing bouncer. Look closer when you see many events from one source, repeated attempts on the same account, or an exploit/RCE scenario — those deserve a closer look. edge-sentinel never auto-blocks: a block is staged and waits for human approval; executing it from this UI needs an operator credential and an enable flag, and the demo's approved block is a dry run (nothing is blocked). Never describe the network as safe or clean; describe what was read and when.

Beyond the network, edge-sentinel also inspects the SOFTWARE SUPPLY CHAIN — the open-source libraries and images this platform installs — BEFORE they run for clients. A supply-chain SCAN (Trivy) checks every dependency, container image and config for known VULNERABILITIES (CVEs), leaked SECRETS and MISCONFIGURATIONS, and builds an SBOM (a Software Bill of Materials: the full list of components). Each CVE names the package, its SEVERITY (critical/high/medium/low), and the FIXED VERSION to upgrade to — the "patched pin" to move to. This is the PRE-DEPLOYMENT half of security; the network monitoring above is the RUNTIME half. In this deployment the scan covers the sentinel's own container filesystem only; scans of other apps' images need docker access that is off by default and are not available in the public demo. Scanner output is a report of known vulnerabilities, not confirmed exploitation.

NETWORK ACCESS (NetBird) is the "who can reach what" half of security — a private WireGuard network with central policies, single sign-on and device checks. CrowdSec guards the perimeter; NetBird controls internal access. edge-sentinel can REVIEW it — ask "review network access" or "check device posture" and it lists the peers (devices) and policies, flagging any over-broad "All-to-All" rule that lets everything reach everything (that breaks least-privilege), plus posture issues like expired logins or unapproved devices. It can also DRAFT an additional, narrower policy, but like a block it is staged for your approval and only applied when you approve it. Applying it only adds a policy: the broad rule stays until someone disables it in NetBird. HOW TO ENABLE LIVE DATA: create a NetBird personal-access token and set NETBIRD_API_TOKEN (for a self-hosted NetBird also set NETBIRD_API_URL to https://your-netbird-host/api). Until a token is set the review shows clearly-labelled SAMPLE data so you can see the shape; once set it reads your real peers and policies.

BACKUPS / CONTINUITY (Portabase) answer "is every database safely backed up, and could we recover?". Portabase is a self-hosted backup control plane: small agents sit next to each database, take an ENCRYPTED dump, and ship it to one or more STORAGE DESTINATIONS — a local disk for a fast restore, and an OFFSITE S3 bucket (AWS S3 or MinIO) for durability if a whole host is lost. edge-sentinel reports COVERAGE — ask "are the databases backed up?" and it says which cores have a recent, encrypted, offsite backup and where the gaps are (no backup at all, local-only with no offsite copy, or unencrypted). HOW TO SET UP OFFSITE S3 BACKUPS: bring up the backup control plane, add each database as a source and an S3 storage destination (keep the real keys in Vault), enrol the backup agent, then set PORTABASE_API_TOKEN so edge-sentinel can read coverage. Tip: add BOTH a local and an S3 destination and fan each backup out to both. Until a token is set, coverage shows clearly-labelled SAMPLE data.

RUNTIME EXPOSURE (deploy-scan) is the complement of the supply-chain scan: it actively scans a LIVE deployment (a host or URL) with nmap / nuclei / ZAP and reports what the scanners find (scanner output, not confirmed exploitation). Supply-chain scanning inspects code & packages BEFORE deploy; this is the RUNTIME half. Because it sends probe traffic it is destructive, so starting a scan is staged for your approval, and scope is fail-closed to an allowlist on the deploy-scan side. It is available only once DEPLOY_SCAN_MCP_URL points at the deploy-scan service, and it cannot be started from the public demo chat."""


def _t_threat_summary(_a: dict) -> dict:
    d = fetch_activity()
    if not d["connected"]:
        return {"text": "CrowdSec core is unreachable, so I can't read live detections right now.", "data": d}
    if not (_src(d, "decisions")[0] and _src(d, "alerts")[0]):
        return {"text": f"CrowdSec was only partly read: {_read_note(d)}. I can say nothing about the part that was not read.",
                "data": {"kpis": d["kpis"], "counts": d["counts"]}}
    k = {x["label"]: x["value"] for x in d["kpis"]}
    c = d["counts"]
    top = ", ".join(f"{s['scenario']} ({s['count']})" for s in d["top_scenarios"][:3]) or "none"
    return {"text": f"{k.get('Active decisions')} active decision(s) held in CrowdSec (read at {d.get('read_at') or 'an unknown time'}), {k.get('Alerts (24h)')} alert(s) in the last 24h "
            f"({c['alerts']} total in store) from {c['sources']} unique source IP(s). Top scenarios: {top}.",
            "data": {"kpis": d["kpis"], "top_scenarios": d["top_scenarios"], "counts": c}}


def _t_list_bans(_a: dict) -> dict:
    d = fetch_activity()
    bans = d["decisions"]
    if not _src(d, "decisions")[0]:
        return {"text": f"I could not read CrowdSec's decisions ({_src(d, 'decisions')[1]}), so I cannot say who is banned.", "data": []}
    if not bans:
        return {"text": f"CrowdSec holds no active block decisions (read at {d.get('read_at') or 'an unknown time'}).", "data": []}
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


def _vuln_principal(role: str = "", scope: str = ""):
    """The Doris vuln-DB principal (role + row-scope) for an outgoing advisory query.

    Named `_vuln_principal`, NOT `_principal`: a second `_principal(request)` exists further down this
    module (the AUTHENTICATED OPERATOR NAME for the case-response route). Two module-level defs of one
    name bind the LAST one, so every `_principal()` call here resolved to the operator helper and raised
    `TypeError: _principal() missing 1 required positional argument: 'request'` — silently swallowed by the
    `except Exception` around scan enrichment, and a hard 400 on `vuln_db_lookup` — whenever Doris was
    reachable. Test: test_vuln_db_principal.py (P7)."""
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


# ─── Runtime data plane: ClamAV (malware scan) + Wazuh (manager status only) ───
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


CLAMAV_SCAN_BASE = os.environ.get("CLAMAV_SCAN_BASE", "/scan")


def _safe_scan_path(raw, base: str | None = None) -> str | None:
    """Validate an LLM-supplied ClamAV scan path. The model's tool args are untrusted: the path must be an
    absolute POSIX path that, once normalised (no ``..``), lies at or under the allow-listed base dir
    (CLAMAV_SCAN_BASE, default ``/scan``), with no control characters (a newline would inject a second clamd
    command). Returns the normalised path, or None when it is rejected."""
    import posixpath
    base = posixpath.normpath(base or CLAMAV_SCAN_BASE)
    if raw in (None, ""):
        return base
    if not isinstance(raw, str) or len(raw) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return None
    if not raw.startswith("/") or ".." in raw.split("/"):
        return None
    p = posixpath.normpath(raw)
    return p if (p == base or p.startswith(base.rstrip("/") + "/")) else None


def _t_malware_scan(a: dict) -> dict:
    # clamd sees the stack at /scan (host /projects mounted read-only in the clamav container).
    path = _safe_scan_path((a or {}).get("path"))
    if path is None:
        return {"text": f"Refused: scan path must be an absolute path under {CLAMAV_SCAN_BASE}.",
                "data": {"refused": True}}
    # Demo mode opens no socket. This one matters more than it looks: a scan is the app asking another
    # container to walk a host filesystem, so a demo that reached a live clamd would have read real files
    # on the operator's machine while presenting the answer as fixture output.
    _demo_read = fixture_mode.read_refusal("ClamAV scan")
    if _demo_read:
        return {"text": _demo_read, "data": {"refused": "demo_mode", "synthetic": True}}
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
    # Demo mode reads nothing outside the process. `{}` is this helper's existing "not reachable" shape,
    # which `_t_wazuh_status` already renders as an honest unavailable answer.
    if fixture_mode.read_refusal("Wazuh agent counts"):
        return {}
    # Certificate verification is ON by default (P9). Wazuh ships a self-signed certificate, so the old
    # unconditional verify=False turned every deployment into a no-verify deployment. An operator with a
    # private CA should set WAZUH_TLS_VERIFY to a CA bundle path (httpx accepts one); WAZUH_TLS_VERIFY=off
    # is the deliberate opt-out, and _t_wazuh_status says so in the answer the operator reads.
    _verify = _security.tls_verify_or_ca("WAZUH_TLS_VERIFY")
    try:
        if not _WZ_TOKEN["t"]:
            r = httpx.get(WAZUH_API + "/security/user/authenticate", auth=(WAZUH_USER, WAZUH_PASS), verify=_verify, timeout=10.0)
            _WZ_TOKEN["t"] = r.json()["data"]["token"]
        h = {"Authorization": "Bearer " + _WZ_TOKEN["t"]}
        return httpx.get(WAZUH_API + path, headers=h, verify=_verify, timeout=10.0).json()
    except Exception:  # noqa: BLE001
        _WZ_TOKEN["t"] = None
        return {}


def _t_wazuh_status(_a: dict) -> dict:
    d = _wazuh_get("/agents/summary/status")
    if not d:
        return {"text": "Wazuh is not reachable from here (set WAZUH_API_PASSWORD). Edge Sentinel only reads Wazuh's "
                "enrolled-agent counts; it does not ingest Wazuh alerts.", "data": {}}
    data = (d.get("data") or {})
    c = data.get("connection", data)   # /agents/summary/status nests counts under .connection
    active, disc = c.get("active", 0), c.get("disconnected", 0)
    total = c.get("total", active + disc)
    return {"text": f"Wazuh manager is reachable: {active} active agent(s), {disc} disconnected, {total} enrolled "
            f"(agent counts only; no Wazuh alerts or detections are read). "
            + ("No endpoints are enrolled as Wazuh agents." if total == 0 else "")
            + _security.tls_verify_note("WAZUH_TLS_VERIFY"),
            "data": {**c, "tls_verified": _security.tls_verify("WAZUH_TLS_VERIFY")}}


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


_SCAN_UNAVAILABLE = ("App image scanning is not available in this deployment: it needs Docker access, which is off by default "
                     "(planned).")


def _t_scan_app(a: dict) -> dict:
    if _SCAN is None:
        return {"text": "Supply-chain scanning is unavailable (context-runtime missing).", "data": {}}
    a = a or {}
    # tolerate the various keys an LLM picks (app / app_name / name / target / value)
    app = next((str(a[k]) for k in ("app", "app_name", "name", "target", "value", "application") if a.get(k)), "")
    cont = _resolve_app_container(app)
    if not cont and not _SCAN.list_scannable_containers(""):
        return {"text": _SCAN_UNAVAILABLE, "data": {"available": False}}
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
            enrich = _VDB.enrich(r.findings, _vuln_principal())   # cross-reference our NVD/OSV vuln-DB
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
    if not conts:
        return {"text": _SCAN_UNAVAILABLE, "data": {"available": False}}
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
    if not conts:
        return {"text": _SCAN_UNAVAILABLE, "data": {"available": False}}
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
    rows = _VDB.lookup(package=pkg or None, cve=cve or None, principal=_vuln_principal())
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


def _build_sentinel_console(console_cls, _crtool):
    """Build the chat console (also called by the tests with a recording stand-in, so the REAL tool text and primer are checked)."""
    return console_cls(
        f"{TENANT} Edge Sentinel", _SENTINEL_PRIMER,
        tools=[
            _crtool("threat_summary", "what CrowdSec currently holds: active decisions (bans), alerts in the last 24h and the top scenarios, with the time of the read. It is a reading, not an assessment of the environment", _t_threat_summary),
            _crtool("list_bans", "list the active ban decisions CrowdSec holds: source IP, scenario, severity and when each expires (a decision is not proof that a bouncer enforces it)", _t_list_bans),
            _crtool("explain_alert", "explain a specific alert currently in CrowdSec's feed (by scenario name) and whether it needs a closer look", _t_explain_alert,
                    parameters={"type": "object", "properties": {"scenario": {"type": "string", "description": "a scenario name from the live feed to explain"}}}),
            _crtool("ban_ip", "stage a ban of an IP for a duration. It is staged for human approval and never runs from this chat; executing a block from this UI needs an operator credential and an enable flag, and the demo's approved block is a dry run (nothing is blocked)", _t_ban_ip, side_effecting=True,
                    parameters={"type": "object", "properties": {"ip": {"type": "string"}, "duration": {"type": "string", "description": "e.g. 4h, 24h"}}}),
            # supply-chain (pre-deploy) tools. The launchers (supply_chain_scan, list_vulnerabilities, scan_app, scan_apps, sbom, malware_scan) start
            # processes / scans, so they are registered side_effecting=True (classified `write`): the approval policy applies in addition to the
            # write grant. list_scannable_apps, scanner_status, explain_vulnerability only read.
            _crtool("supply_chain_scan", "scan the sentinel's own container filesystem for known vulnerabilities with Trivy (a scanner report, not confirmed exploitation). Scanning other apps' images needs docker access, which is off by default and not available in the public demo", _sc_scan,
                    parameters={"type": "object", "properties": {"target": {"type": "string", "description": "path or image to scan (default: the stack's dependencies)"}}}, side_effecting=True),
            _crtool("list_vulnerabilities", "list the CVEs found in the last scan, worst first, each with the fixed version where there is one (a scanner report; run supply_chain_scan first)", _sc_list, side_effecting=True),
            _crtool("explain_vulnerability", "explain a specific CVE from the last scan and the fixed version to move to, if there is one", _sc_explain,
                    parameters={"type": "object", "properties": {"id": {"type": "string", "description": "a CVE id from the scan"}}}),
            _crtool("scan_app", "Not available: app image scans need Docker access, which is off by default and is not provided in the public demo (planned).", _t_scan_app,
                    parameters={"type": "object", "properties": {"app": {"type": "string", "description": "the app to scan, e.g. billing, compliance, market-radar, books"}}}, side_effecting=True),
            _crtool("scan_apps", "Not available: app image scans need Docker access, which is off by default and is not provided in the public demo (planned).", _t_scan_apps,
                    parameters={"type": "object", "properties": {
                        "ecosystem": {"type": "string", "description": "restrict findings to one ecosystem: npm | pypi | maven | go | os (optional)"},
                        "apps": {"type": "string", "description": "comma-separated app names to scan (optional; default: all agentic apps)"},
                        "limit": {"type": "integer", "description": "max apps to scan in one call (default 8)"}}}, side_effecting=True),
            _crtool("list_scannable_apps", "Not available: app image scans need Docker access, which is off by default and is not provided in the public demo (planned).", _t_list_apps),
            _crtool("vuln_db_lookup", "look up a package or CVE in the NVD/OSV vulnerability table (Apache Doris) when it is reachable; the table holds only what its refresh job was configured to load, not all of NVD or OSV", _t_vuln_lookup,
                    parameters={"type": "object", "properties": {"package": {"type": "string"}, "cve": {"type": "string", "description": "a CVE id"}}}),
            _crtool("vuln_db_permissions", "demonstrate the vuln-DB access policy: the same query returns different rows and columns per principal (row scope and column masking)", _t_vuln_db_permissions),
            _crtool("sbom", "list the components (a software bill of materials) of the sentinel's own filesystem with Syft or Trivy", _sc_sbom, side_effecting=True),
            _crtool("scanner_status", "which scanners (trivy, syft, cosign, clamscan) are installed in this container and the last scan result", _sc_status),
            # runtime-DAST (post-deploy) tools — scan a LIVE deployment via the deploy-scan service
            _crtool("scan_deployment", "Not available from this chat (planned): a deployment scan would send probe traffic, so it would be staged for human approval; it never runs here.", deploy_scan.scan, side_effecting=True,
                    parameters={"type": "object", "properties": {
                        "target": {"type": "string", "description": "host or URL to scan (must be in the deploy-scan allowlist)"},
                        "profile": {"type": "string", "description": "recon | web-baseline | web-active | full (default web-baseline)"}}}),
            _crtool("scan_status", "read the status of a deployment scan job by its job id (no scan can be started from this chat)", deploy_scan.status,
                    parameters={"type": "object", "properties": {"job_id": {"type": "string"}}}),
            _crtool("scan_results", "read the findings of a finished deployment scan job by its job id (no scan can be started from this chat)", deploy_scan.results,
                    parameters={"type": "object", "properties": {"job_id": {"type": "string"}}}),
            # runtime data plane
            _crtool("malware_scan", "scan files visible to the ClamAV service with ClamAV (only where a ClamAV service is configured)", _t_malware_scan,
                    parameters={"type": "object", "properties": {"path": {"type": "string", "description": "path to scan (default: the whole stack)"}}}, side_effecting=True),
            _crtool("wazuh_status", "report the Wazuh manager's enrolled-agent counts (status only; no Wazuh alerts or detections are read)", _t_wazuh_status),
            # network access plane (NetBird / zero-trust) — the "who can reach what" half
            _crtool("network_access_review", "review NetBird peers and policies, flagging an over-broad All-to-All rule; SAMPLE DATA unless NetBird is connected", netbird.access_review),
            _crtool("network_posture", "check NetBird device posture (expired logins, unapproved devices, broad SSH); SAMPLE DATA unless NetBird is connected", netbird.posture),
            _crtool("propose_network_policy", "draft an additional, narrower NetBird access policy for an over-broad one (staged for human approval; applying it only adds a policy and does not remove or disable the broad one; sample data unless NetBird is connected)", netbird.propose, side_effecting=True,
                    parameters={"type": "object", "properties": {"policy": {"type": "string", "description": "name of the over-broad policy to draft a narrower policy for (optional; default: the first one found)"}}}),
            # continuity plane (Portabase / backups) — is every core safely backed up offsite?
            _crtool("backup_status", "report Portabase backup coverage (recent, encrypted, offsite) and the gaps; SAMPLE DATA unless Portabase is connected", portabase.backup_status),
        ],
        suggestions=_chips(),
        subtitle="Ask what CrowdSec holds, to explain an alert, to look up a CVE, or to read the sample network and backup reviews.",
        model=_GovernedConsoleModel(),
        allow_side_effects=[],  # ban_ip stays gated → the harness asks for human approval before blocking
    )


_SENTINEL_CONSOLE = _build_sentinel_console(_AgentConsole, _crtool) if _AgentConsole is not None else None


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    sh = live.state_health()
    # core.crowdsec_connected(), not the name imported at module load: in fixture mode that alias still
    # points at the original function, and a health endpoint that reports the pre-swap function would
    # be reporting a connection that does not exist.
    return _with_authorization(_GATE_APP, {"status": "ok" if sh["ok"] else "degraded", "core": "crowdsec",
            "connected": core.crowdsec_connected(), "demo": fixture_mode.status(),
            "state": sh})


@app.post("/api/agent", dependencies=[Depends(ratelimit.rate_limit)])
async def api_agent(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        body = {}
    if _SENTINEL_CONSOLE is None:
        return JSONResponse({"intent": "help", "text": "The assistant is offline (context-runtime unavailable).", "evidence": []})
    return JSONResponse(_SENTINEL_CONSOLE.respond((body or {}).get("message", ""), principal=_req_principal(request)))


@app.get("/api/activity")
def activity() -> JSONResponse:
    if (_r := _deny_response(_GATE_APP, "threat_summary")):
        return _r
    return JSONResponse(fetch_activity())


@app.post("/api/permissions/query", dependencies=[Depends(ratelimit.rate_limit)])
async def permissions_query(request: Request) -> JSONResponse:
    """Run the vuln query as full-access (left) and the posted scoped grant (right)."""
    if (_r := _deny_response(_GATE_APP, "vuln_db_permissions")):
        return _r
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        body = {}
    return JSONResponse(permissions_ui.query(body or {}))


@app.get("/api/permissions/ui", response_class=HTMLResponse)
def permissions_ui_page() -> str:
    if (_r := _deny_response(_GATE_APP, "vuln_db_permissions")):
        return _r
    return permissions_ui.PERM_PAGE


@app.get("/api/network/review")
def network_review() -> JSONResponse:
    """Zero-trust network access review over NetBird (live when configured, else sample)."""
    if (_r := _deny_response(_GATE_APP, "network_access_review")):
        return _r
    return JSONResponse(netbird.access_review({})["data"])


@app.get("/api/network/ui", response_class=HTMLResponse)
def network_ui() -> str:
    if (_r := _deny_response(_GATE_APP, "network_access_review")):
        return _r
    return netbird.panel()


@app.get("/api/backups/status")
def backups_status() -> JSONResponse:
    """Backup coverage over Portabase (live when configured, else sample)."""
    if (_r := _deny_response(_GATE_APP, "backup_status")):
        return _r
    return JSONResponse(portabase.backup_status({})["data"])


@app.get("/api/backups/ui", response_class=HTMLResponse)
def backups_ui() -> str:
    if (_r := _deny_response(_GATE_APP, "backup_status")):
        return _r
    return portabase.panel()


@app.get("/api/cases")
def api_cases() -> JSONResponse:
    """List live investigation cases, worst severity first. Syncs the latest CrowdSec alerts into cases first
    (idempotent), so the list reflects real detections — never a canned scenario."""
    if (_r := _deny_response(_GATE_APP, "threat_summary")):
        return _r
    live.sync_cases()
    return JSONResponse({"cases": live.case_summaries()})


@app.get("/api/cases/{case_id}")
def api_case(case_id: str) -> JSONResponse:
    """Full resolved bundle for one case (evidence/observations/findings/actions/decisions/receipts) plus
    the replay proof that the case id is reproducible from its immutable evidence."""
    if (_r := _deny_response(_GATE_APP, "threat_summary")):
        return _r
    detail = live.case_detail(case_id)
    if detail is None:
        return JSONResponse({"error": f"unknown case '{case_id}'"}, status_code=404)
    return JSONResponse(detail)


def _principal(request: Request) -> "str | None":
    """The authenticated operator name, or None when the request carries no valid operator credentials.
    Never derived from a request body."""
    try:
        return auth.require_operator(request)
    except HTTPException:
        return None


@app.post("/api/cases/{case_id}/respond", dependencies=[Depends(ratelimit.rate_limit)])
async def api_case_respond(case_id: str, request: Request) -> JSONResponse:
    """DEMO-FACING decision — OPEN, no auth, NO edge effect. Reject records the rejection and stops; approve
    stages the action as APPROVED (awaiting the authenticated execute step). The edge is never touched here —
    the real kick-off is POST /api/cases/{id}/execute, which is operator-authenticated."""
    if (_r := _deny_response(_GATE_APP, "ban_ip")):
        return _r
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        body = {}
    request_id = (body or {}).get("request_id", "")
    if not request_id:
        return JSONResponse({"error": "missing 'request_id'"}, status_code=400)
    try:
        approved = bool((body or {}).get("approved"))
        principal = _principal(request)
        if principal is None:       # anonymous: simulated preview, nothing is recorded
            out = live.preview_decide(case_id, request_id, approved=approved)
        else:
            out = live.decide(case_id, request_id, approved=approved, actor=f"operator:{principal}")
    except KeyError:
        return JSONResponse({"error": f"unknown case or request ({case_id}/{request_id})"}, status_code=404)
    except live.ExecutionError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
    return JSONResponse(out)


@app.post("/api/cases/{case_id}/execute")
async def api_case_execute(case_id: str, request: Request,
                           operator: str = Depends(auth.require_operator)) -> JSONResponse:
    """AUTHENTICATED execution — the real kick-off. HTTP-Basic operator gate (503 if unconfigured, 401 if not
    authenticated). Only an already-APPROVED action runs; dry-run unless SENTINEL_BLOCK_ENABLED, so the public
    demo never mutates the live edge by default. Returns the receipt (execution proof) + a distinct verification."""
    if (_r := _deny_response(_GATE_APP, "ban_ip")):
        return _r
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        body = {}
    request_id = (body or {}).get("request_id", "")
    if not request_id:
        return JSONResponse({"error": "missing 'request_id'"}, status_code=400)
    try:
        tok = ipguard.OPERATOR_IP.set(ratelimit.client_ip(request))   # never ban the operator's own address
        try:
            out = live.execute(case_id, request_id, actor=f"operator:{operator}")
        finally:
            ipguard.OPERATOR_IP.reset(tok)
    except KeyError:
        return JSONResponse({"error": f"unknown case or request ({case_id}/{request_id})"}, status_code=404)
    except live.ExecutionError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except fixture_mode.DemoRefused:
        raise  # a policy refusal, not a client error: the registered handler answers 403 refused=demo_mode
    except Exception as e:  # noqa: BLE001 — e.g. GovernanceError refusing an ungoverned action
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
    return JSONResponse(out)


def _cr_banner() -> str:
    """The sticky banner; its Network access and Backups links say sample while the panel behind them shows sample data."""
    nb = " (sample data)" if _is_sample("netbird") else ""
    pb = " (sample data)" if _is_sample("portabase") else ""
    return _CR_BANNER.replace("@@NB@@", nb).replace("@@PB@@", pb)


_CR_BANNER = """<div style="position:sticky;top:0;z-index:9998;background:linear-gradient(90deg,#10201d,#17171a);border-bottom:1px solid #2f2f33;color:#e4e2e6;font:13px/1.4 Roboto,system-ui,sans-serif;padding:9px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap"><span style="background:#4fd1c5;color:#08110f;font-weight:700;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">CONTEXT RUNTIME</span><span style="background:#2f2f33;border-radius:5px;padding:2px 8px;font-size:11px;letter-spacing:.4px">DEMO</span><span style="color:#9b99a1">This app runs on <b style="color:#e4e2e6">Context Runtime</b>, which powers the investigation assistant and the permissions plane. <a href="https://github.com/redevops-io/context-runtime" style="color:#4fd1c5;text-decoration:none">learn more \u2192</a></span><span style="margin-left:auto;display:flex;gap:8px;flex-wrap:wrap"><a href="api/network/ui" style="background:#1b2b28;color:#4fd1c5;border:1px solid #2f6f66;font-weight:700;border-radius:6px;padding:5px 11px;font-size:12px;text-decoration:none;white-space:nowrap">\U0001F310 Network access@@NB@@ \u2192</a><a href="api/backups/ui" style="background:#1b2b28;color:#4fd1c5;border:1px solid #2f6f66;font-weight:700;border-radius:6px;padding:5px 11px;font-size:12px;text-decoration:none;white-space:nowrap">\U0001F5C4 Backups@@PB@@ \u2192</a><a href="api/permissions/ui" style="background:#4fd1c5;color:#08110f;font-weight:700;border-radius:6px;padding:5px 11px;font-size:12px;text-decoration:none;white-space:nowrap">\U0001F512 Permissions demo \u2192</a></span></div>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    if (_r := _deny_response(_GATE_APP, "threat_summary")):
        return _r
    import re as _cr_re
    page = render(fetch_activity())
    blocks = ""
    _demo = fixture_mode.status()
    if _demo["enabled"]:
        # The label is on the page, not only in a config file: a screenshot of a demo must be
        # self-identifying, because a screenshot is the thing that outlives the conversation about it.
        # Audit 2026-11-11 (PR #613 review): the sentence used to speak only of CrowdSec, which was true but
        # narrower than what a reader took from it — NetBird, Portabase, deploy-scan, Wazuh, ClamAV and the
        # model were not covered. The boundary now reaches all of them, so the sentence says what the code
        # guarantees instead of one integration's share of it.
        blocks += ("<section class='shell' style='margin-top:var(--sp-4)'><div role='status' class='panel' "
                   "style='border-left:4px solid #b45309;padding:var(--sp-3)'>"
                   f"<strong>{html.escape(_demo['tag'])}</strong> — every alert, decision and figure on this "
                   "page is synthetic fixture data. No CrowdSec core was read to produce it, and no side "
                   "effect is performed from this page in demo mode: CrowdSec, NetBird, Portabase, "
                   "deploy-scan, Wazuh, ClamAV and the model are all held back, so nothing here reads or "
                   "changes a live service. Unset "
                   f"<code>{html.escape(fixture_mode.DEMO_ENV)}</code> to operate against real "
                   "integrations.</div></section>")
    try:
        live.sync_cases()
        blocks += ("<section class='shell' style='margin-top:var(--sp-4)'>"
                   + _cases_panel(live.case_summaries()) + "</section>")
    except Exception:  # noqa: BLE001 — the case workspace is additive; never break the dashboard
        pass
    if _SENTINEL_CONSOLE is not None:
        _SENTINEL_CONSOLE.suggestions = _chips()          # the sample label follows the panel's real state
        blocks += "<section class='shell' style='margin-top:var(--sp-4)'>" + _SENTINEL_CONSOLE.panel_html("sentinel") + "</section>"
    if blocks:
        page = page.replace("<footer", blocks + "<footer", 1)
    page = _cr_re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + _cr_banner(), page, count=1)
    if "CONTEXT RUNTIME" not in page:
        page = _cr_banner() + page
    return page


@app.post("/agent/run", dependencies=[Depends(ratelimit.rate_limit)])
async def agent_run(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:
        body = {}
    action = (body or {}).get("action", "")
    _tool = {"block_ip": "ban_ip", "approve_block": "ban_ip", "triage": "sentinel.triage", "propose_network_policy": "propose_network_policy",
             "approve_network_policy": "propose_network_policy"}.get(action) if isinstance(action, str) else None
    if _tool and (_r := _deny_response(_GATE_APP, _tool)):                          # authorise where the tool body runs (also before the operator Basic check)
        return _r

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
        tok = ipguard.OPERATOR_IP.set(ratelimit.client_ip(request))
        try:
            return JSONResponse(_approve_block(body or {}))
        finally:
            ipguard.OPERATOR_IP.reset(tok)
    if action == "triage":
        return JSONResponse(_triage(body or {}))
    # NetBird — propose a tighter policy (staged) then apply it on approval
    if action == "propose_network_policy":
        return JSONResponse(netbird.propose(body or {}))
    if action == "approve_network_policy":
        if not auth.block_enabled():
            return JSONResponse({"status": "dry_run", "action": "approve_network_policy",
                                 "summary": "Dry run: nothing was changed in NetBird. The policy was not created.",
                                 "detail": "Dry run: SENTINEL_BLOCK_ENABLED is not set, so nothing was changed in NetBird."})
        return JSONResponse(netbird.approve(body or {}))
    return JSONResponse(
        {"status": "error", "error": f"unknown action '{action}'",
         "supported": ["block_ip", "approve_block", "triage", "propose_network_policy", "approve_network_policy"]},
        status_code=400,
    )


if __name__ == "__main__":  # pragma: no cover
    import sys

    import uvicorn

    from agentic_os.app_gate import require_explicit_setting_when_exposed as _exposed

    try:
        _security.exposure_guard(BIND_HOST)   # P9: a reachable bind is something you ask for, twice over —
        _exposed(BIND_HOST)                   # once here (credentials) and once in the kernel (permissions)
    except _security.ExposureRefused as _refused:
        # The application exception becomes the user-facing startup error here, at the process boundary:
        # nothing is printed to the dashboard, nothing binds a port, the shell gets exit code 1.
        sys.exit(f"edge-sentinel: startup refused: {_refused}")

    uvicorn.run(app, host=BIND_HOST, port=PORT)


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
