"""agentic-compliance — agent layer + MD3 dashboard over a REAL OpenSCAP core.

Sibling of agents/billing (the reference pattern), but the "core" is a self-contained
compliance **scanner** rather than a long-running server: the OpenSCAP `oscap` tool
produces REAL XCCDF results (rule id, title, result pass/fail/notapplicable, severity)
from a CIS Ubuntu 22.04 Level 1 - Server benchmark, and this agent monitors/explains
them — no cloud creds.

Pattern (same as billing):
  1. point at the real core output (here: the cached SCAP results file),
  2. write a `fetch_*` that parses REAL records + a `compute_kpis`,
  3. reuse BASE_CSS + the render helpers,
  4. add agentic actions in /agent/run that are deterministic, with a human-approval
     gate on anything that changes the system (remediate -> policy_change).

Endpoints:
  GET  /health        -> {"status","core":"openscap","connected": <results file present?>}
  GET  /api/activity  -> REAL parsed findings: pass rate %, passing/failing counts,
                         top failing rules by severity, grouped framework view, plus a
                         couple of SME compliance items (license/insurance expiry).
  GET  /              -> MD3 compliance dashboard (Vanta/Drata style) from the REAL scan.
  GET  /report        -> the OpenSCAP-generated HTML report.
  POST /agent/run     -> {"action": "scan" | "explain" | "remediate" | "file_consent"}

Config (env; seed.py writes agents/compliance/.env automatically):
  SCAP_RESULTS   path to the real oscap XCCDF results file (default: results/scan-results.xml)
  SCAP_REPORT    path to the real oscap HTML report   (default: results/report.html)
  SCAP_PROFILE   the xccdf profile id that was evaluated
  PORT           uvicorn port, default 8208
  REDEVOPS_LLM_BASE_URL  OPTIONAL in-boundary model for the "explain" rewrite (agentic_os.app.llm.governed_text,
                     INTERNAL, fail-closed); the endpoint works fully without it (deterministic fallback from
                     the rule's own description/rationale/fix in the SCAP content).
  OPERATOR_BASIC_AUTH  ``user:pass`` -- a request carrying these Basic credentials is the operator's approval for
                     remediate / file_consent; everything else only gets a staged preview.
"""
from __future__ import annotations

import html
import os

# ── enterprise permissions plane — gate tool/data access by (app, user) ──
# No-op without the context_runtime_enterprise package or CR_PERMISSIONS=1 (open-core default).
try:
    from context_runtime_enterprise.apps import bootstrap as _cr_bootstrap
    _req_principal = _cr_bootstrap("compliance")
except Exception:  # noqa: BLE001
    def _req_principal(request=None):
        return None
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

# --- config ------------------------------------------------------------------
# The OpenSCAP results parser, KPIs and agentic actions live in core.py (pure — no web /
# context-runtime deps) so the Mission Runtime operator can invoke them and they can be
# tested against a fixture results file. Import config + core helpers from there; core
# loads .env.
from . import core
from .gate import operator_approval
from .core import (  # noqa: E402
    SCAP_REPORT, SCAP_PROFILE, TENANT, SUBTITLE, FRAMEWORK,
    scap_connected, fetch_activity,
)

PORT = int(os.environ.get("PORT", "8208"))

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


app = FastAPI(title=f"agentic-compliance ({TENANT} · core: OpenSCAP)")

from agentic_os.app_kit import route_authz as _route_authz  # noqa: E402
from .route_policy import APP as _APP, ROUTE_POLICIES as _ROUTE_POLICIES  # noqa: E402
_route_authz.install(app, _APP, _ROUTE_POLICIES)   # one enforcement layer for every write route


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
.barlist__row{display:grid;grid-template-columns:1fr 1fr 88px;align-items:center;gap:var(--sp-4)}
.barlist__label{color:var(--on-surface-variant);font:400 14px/20px var(--font-sans)}
.barlist__pct{text-align:right;font-family:var(--font-mono);font-feature-settings:"tnum";font-size:13px;color:var(--on-surface-variant)}
.fw-card{display:flex;flex-direction:column;gap:var(--sp-3)}
.fw-card__top{display:flex;align-items:baseline;justify-content:space-between;gap:var(--sp-3)}
.fw-card__name{font:500 14px/20px var(--font-sans);color:var(--on-surface)}
.fw-card__pct{font:500 22px/28px var(--font-mono);color:var(--primary);font-feature-settings:"tnum"}
.fw-card__meta{color:var(--on-surface-muted);font:400 12px/16px var(--font-sans)}
.sev{font-family:var(--font-mono);font-size:12px;text-transform:uppercase}
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


def _sev_pill(sev: str) -> str:
    s = (sev or "").lower()
    if s == "high":
        return "pill--danger"
    if s == "medium":
        return "pill--warn"
    if s == "low":
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


def _approval_banner(data: dict) -> str:
    """Surfaced when there are failing controls — the agent can remediate (approval-gated)."""
    failing = data.get("failing", [])
    if not failing:
        return ""
    first = failing[0]
    extra = f" (+{len(failing) - 1} more)" if len(failing) > 1 else ""
    return (
        "<div class='banner'>"
        f"<span class='pill pill--warn'><span class='pill__dot'></span>{len(failing)} failing</span>"
        "<span class='label-m' style='text-transform:uppercase;color:var(--warning)'>policy_change</span>"
        f"<span class='body-m'>Top finding [{_esc((first['severity'] or '').upper())}]: "
        f"{_esc(first['title'])}{_esc(extra)}. The agent can stage a remediation, but applying "
        "system fixes is approval-gated (never auto-applied).</span>"
        "</div>"
    )


def _framework_cards(data: dict) -> str:
    """Vanta/Drata-style framework-status cards with progress bars."""
    cards = ""
    for fw in data["frameworks"]:
        pct = fw["pct"]
        cards += (
            "<div class='fw-card'>"
            "<div class='fw-card__top'>"
            f"<span class='fw-card__name'>{_esc(fw['name'])}</span>"
            f"<span class='fw-card__pct'>{pct}%</span>"
            "</div>"
            f"<div class='bar'><span style='width:{pct}%'></span></div>"
            f"<div class='fw-card__meta'>{fw['passing']} passing · {fw['failing']} failing · "
            f"{fw['notapplicable']} not applicable</div>"
            "</div>"
        )
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Framework status</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>data: live from OpenSCAP</span></div>"
        f"{cards}"
        "</div>"
    )


def _failing_table(data: dict) -> str:
    """The expiring/failing-items queue: real failing CIS controls + expiring SME items."""
    rows = ""
    for f in data["failing"]:
        sev = (f["severity"] or "unknown").upper()
        rows += (
            "<tr>"
            f"<td>{_esc(f['title'])}</td>"
            f"<td><span class='pill pill--neutral sev'>CIS</span></td>"
            f"<td><span class='pill {_sev_pill(f['severity'])}'>{_esc(sev)}</span></td>"
            "<td><span class='pill pill--danger'>FAIL</span></td>"
            "</tr>"
        )
    for s in data["sme"]:
        if not s["expiring"]:
            continue
        rows += (
            "<tr>"
            f"<td>{_esc(s['item'])} <span class='fw-card__meta'>· expires {_esc(s['expires'])}</span></td>"
            f"<td><span class='pill pill--neutral sev'>{_esc(s['kind'].upper())}</span></td>"
            "<td><span class='pill pill--warn'>MEDIUM</span></td>"
            f"<td><span class='pill pill--warn'>{_esc(s['days'])}d</span></td>"
            "</tr>"
        )
    if not rows:
        rows = "<tr><td colspan='4' class='fw-card__meta'>No failing or expiring items — all controls met.</td></tr>"
    return (
        "<div class='card'>"
        "<div class='card__head'><h2 class='card__title'>Failing &amp; expiring queue</h2>"
        "<span class='pill pill--info'><span class='pill__dot'></span>real oscap findings</span></div>"
        "<table class='table'><thead><tr><th>Control / item</th><th>Source</th><th>Severity</th>"
        "<th>Status</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        "</div>"
    )


def render(data: dict) -> str:
    connected = data["connected"]
    conn_txt = "core: OpenSCAP connected" if connected else "core: OpenSCAP NO RESULTS"
    conn_cls = "pill--success" if connected else "pill--danger"
    status_pill = (
        f"<span class='pill {conn_cls}'><span class='pill__dot'></span>agent active · {_esc(conn_txt)}</span>"
    )
    live_badge = "<span class='pill pill--info'><span class='pill__dot'></span>data: live from OpenSCAP</span>"
    open_btn = f"<a class='btn' href='{_esc(data['report_url'])}' target='_blank' rel='noopener'>Open report ↗</a>"

    body = (
        _approval_banner(data)
        + _kpi_tiles(data["kpis"])
        + "<section class='shell' style='gap:var(--sp-4)'>"
        "<div class='section-label'>Control posture</div>"
        "<div class='grid'>"
        f"<div class='col-5' style='grid-column:span 5'>{_framework_cards(data)}</div>"
        f"<div class='col-7' style='grid-column:span 7'>{_failing_table(data)}</div>"
        "</div></section>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentic Compliance — {_esc(TENANT)}</title>
{FONT_LINK}
<style>{BASE_CSS}{PAGE_CSS}</style>
</head>
<body class="page">
<div class="shell">
  <header class="appbar">
    <div class="appbar__row">
      <h1>Agentic Compliance</h1>
      {status_pill}
      {live_badge}
      <span class="spacer"></span>
      {open_btn}
    </div>
    <div class="appbar__tenant"><b>{_esc(TENANT)}</b> · core: OpenSCAP (open-source compliance scanner)
      · profile: {_esc(data['framework'])}</div>
    <div class="appbar__sub">{_esc(SUBTITLE)}</div>
  </header>
  {body}
  <footer class="footer">agentic-compliance · live findings for {_esc(TENANT)} ·
    <a href="/api/activity">/api/activity</a> · <a href="/report">/report</a> ·
    agent + human, on a real OpenSCAP core · redevops.io Agentic Business OS</footer>
</div>
</body>
</html>"""


# --- optional LLM rewrite (governed; works without any model) ---------------
def _llm_blurb(prompt: str) -> str | None:
    """A plain-English rewrite from the GOVERNED in-boundary model, or None.

    N7: the prompt is public CIS rule text plus this host's findings, classified INTERNAL and routed by
    ``agentic_os.app.llm.governed_text`` -- fail closed (None) when the router refuses or no in-boundary
    endpoint is configured; there is no direct-HTTP fallback. `explain` already has a deterministic answer."""
    from .assistant import governed_answer
    try:
        from agentic_os.governance.classification import DataClassification
    except Exception:  # noqa: BLE001
        return None
    return governed_answer(prompt, (DataClassification.INTERNAL,), max_tokens=220, temperature=0.3)


def _with_approval(body: dict, approval) -> dict:
    """The core call's body: every caller-supplied ``_approval*`` key is stripped, then the route's
    server-side marker -- if any -- is set."""
    clean = _strip_approval(body or {})
    return {**clean, "_approval": approval} if approval else clean


# --- agentic actions (thin wrappers over core; the approval gate lives IN core) ---
# ``approval`` is None for every caller except a route that proved operator Basic auth (see gate.py).
def _scan() -> dict:
    """Re-run the real oscap scan over the core (deterministic; no LLM; changes nothing on the host)."""
    return core.scan()


def _explain(body: dict) -> dict:
    """Explain a failing rule, with the optional governed LLM rewrite blurb layered on."""
    return core.explain(_strip_approval(body or {}), blurb=_llm_blurb)


def _remediate(body: dict, approval=None) -> dict:
    """Open a remediation TICKET (staged unless approved; never a host fix)."""
    return core.remediate(_with_approval(body, approval))


def _file_consent(body: dict, approval=None) -> dict:
    """Onboarding gate -- file the customer's consent record into the evidence ledger (staged unless approved)."""
    return core.file_consent(_with_approval(body, approval))


#: every /agent/run action -> handler(body, approval). The route is the ONLY place the operator marker enters.
ACTIONS = {
    "scan": lambda body, approval: _scan(),
    "explain": lambda body, approval: _explain(body),
    "remediate": _remediate,
    "file_consent": _file_consent,
}


# ──────── conversational agent · Context Runtime (LLM) + redevops-rag (grounding) + agent-harness (tools) ────────
from .assistant import Assistant as _Assistant, Tool as _Tool

_COMPLIANCE_PRIMER = """SCAP (Security Content Automation Protocol) is the open standard OpenSCAP uses to check a system against a security baseline. A scan compares the machine's real configuration to a benchmark and marks every check pass or fail.

A BENCHMARK (XCCDF document) is the full catalogue of security controls — for example the CIS Ubuntu Linux 22.04 baseline. A PROFILE is a named subset of that benchmark for a specific target, e.g. CIS Level 1 Server (safe hardening), CIS Level 2 (stricter), STIG (US DoD), or PCI-DSS (card data). Pick the profile that matches your obligation: Level 1 Server is the sensible default for a general Linux server; only move to Level 2 or STIG if a contract or regulator requires it.

A RULE is a single control inside a profile (e.g. "Ensure SSH root login is disabled"). Each rule carries a title, a description of what it checks, a rationale for why it matters, and a fix. The actual test behind a rule is an OVAL check — the low-level probe that inspects a file, package or setting and returns true/false.

SEVERITY ranks how much a failed rule matters: HIGH (fix first — real exposure), MEDIUM (should fix), LOW (hardening nice-to-have). Always work the failing HIGH-severity rules before medium or low ones.

A result is PASS (the system already meets the control), FAIL (it does not — this is a finding to remediate), or NOTAPPLICABLE (the rule does not apply to this system and is not scored). The control PASS RATE is passing ÷ (passing + failing) scored controls — notapplicable rules are excluded.

To READ A SCAN: start from the pass rate, then open the failing queue sorted by severity. For any failure, read the rule's description (what) and rationale (why), then its fix (how). REMEDIATION is the fix action the content ships — usually a shell (bash) snippet or an Ansible task that changes a config file or setting so a re-scan turns the rule green.

To REMEDIATE a failed rule: apply its fix (bash or Ansible), then re-run the scan to confirm the rule now passes. Because a fix changes the host's configuration, this agent never applies fixes automatically — it stages the exact remediation and asks for human approval (policy_change) first.

Beyond the technical OpenSCAP controls, this tenant also tracks SME compliance items — the firm's regulatory registrations and insurance policies — which appear in the queue as EXPIRING when they are within 90 days of their renewal date."""


def _t_scan_summary(_a: dict) -> dict:
    d = fetch_activity()
    k = {x["label"]: x["value"] for x in d["kpis"]}
    c = d["counts"]
    return {
        "text": f"Control pass rate {k.get('Control pass rate')} across {c['scored']} scored controls — "
                f"{c['pass']} passing, {c['fail']} failing, {c['notapplicable']} not applicable. "
                f"Profile: {d['framework']}.",
        "data": d["kpis"],
    }


def _t_list_failures(_a: dict) -> dict:
    fails = fetch_activity()["failing"]
    if not fails:
        return {"text": "No failing controls — every scored CIS rule passed.", "data": []}
    lines = [f"[{(f['severity'] or 'unknown').upper()}] {f['title']} (id: {f['short']})" for f in fails[:10]]
    more = f"\n(+{len(fails) - 10} more)" if len(fails) > 10 else ""
    return {"text": f"{len(fails)} failing control(s), highest severity first:\n" + "\n".join(lines) + more, "data": fails}


def _t_explain_finding(a: dict) -> dict:
    rule_id = a.get("rule_id") or a.get("rule") or a.get("id") or ""
    if not rule_id and a.get("message"):        # console free text: "explain <rule id>"
        import re as _re
        m = _re.search(r"explain\s+(?:the\s+|rule\s+|control\s+|finding\s+)*([\w.\-]{6,})", a["message"], _re.I)
        rule_id = m.group(1) if m else ""
    if not rule_id:
        fails = fetch_activity()["failing"]
        avail = "\n".join(f"- {f['short']}" for f in fails[:8])
        return {"text": "Which control? Give me a rule id from the failing queue, e.g.:\n" + avail, "data": fails[:8]}
    r = _explain({"rule_id": rule_id})
    if r.get("status") != "done":
        return {"text": f"Couldn't find rule '{rule_id}'. Try a rule id from the failing queue.", "data": r}
    text = r.get("explanation_llm") or r.get("explanation", "")
    return {"text": f"[{(r.get('severity') or 'unknown').upper()}] {r.get('title')}\n\n{text}", "data": r}


def _t_run_scan(_a: dict) -> dict:
    # A fresh scan is long-running: it is run by the operator via /agent/run, not from the chat panel.
    return _t_scan_summary(_a)


try:
    from agentic_os.governance.classification import DataClassification as _DC
    _CLS = (_DC.INTERNAL,)
except Exception:  # noqa: BLE001
    _CLS = ()

# Console tools never remediate or file consent; a fresh scan is run by the operator via /agent/run.
_COMP_CONSOLE = _Assistant(
    f"{TENANT} Compliance", _COMPLIANCE_PRIMER,
    tools=[
        _Tool("scan_summary", "current compliance posture", _t_scan_summary, keywords=("posture", "pass rate", "summary", "compliance")),
        _Tool("list_failures", "list the failing CIS controls", _t_list_failures, keywords=("failing", "failures", "failed controls")),
        _Tool("explain_finding", "explain a failing control", _t_explain_finding, keywords=("explain",)),
        _Tool("run_scan", "latest scan results (a fresh scan is run by the operator)", _t_run_scan, keywords=("scan", "rescan")),
    ],
    suggestions=["What's our compliance posture?", "Show the failing controls", "How do I remediate a failed rule?", "Which CIS profile should I use?"],
    subtitle="Ask about SCAP findings and how to fix them.",
    classifications=_CLS,
)


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "core": "openscap", "connected": scap_connected()}


@app.post("/api/agent")
async def api_agent(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        body = {}
    return JSONResponse(_COMP_CONSOLE.respond((body or {}).get("message", ""), principal=_req_principal(request)))


@app.get("/api/activity")
def activity() -> JSONResponse:
    return JSONResponse(fetch_activity())


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    page = render(fetch_activity())
    if _COMP_CONSOLE is not None:
        panel = "<section class='shell' style='margin-top:var(--sp-4)'>" + _COMP_CONSOLE.panel_html("comp") + "</section>"
        page = page.replace("<footer", panel + "<footer", 1)
    return page


@app.post("/agent/run")
async def agent_run(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:
        body = {}
    action = (body or {}).get("action", "")
    handler = ACTIONS.get(action) if isinstance(action, str) else None
    if handler is None:
        return JSONResponse(
            {"status": "error", "error": f"unknown action '{action}'", "supported": sorted(ACTIONS)},
            status_code=400,
        )
    # Server-side only: valid operator Basic auth => the route injects the approval marker; else staged preview.
    return JSONResponse(handler(body or {}, operator_approval(request)))


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive compliance as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_compliance_operator
    app.include_router(build_compliance_operator().router())
    # Runtime-native: register this app's governed contract at boot (guarded with the mount above).
    from agentic_os.app_kit.boot import register_app_manifest as _register_runtime_native
    from . import manifest as _runtime_native_manifest
    _register_runtime_native(_runtime_native_manifest)
except Exception as _op_exc:  # agentic_os absent / operator build failed — surface it, do not hide it
    import logging as _logging
    _logging.getLogger("agentic_os.app").error(
        "%s: operator/capability surface NOT mounted (%s: %s) — /invoke + /capabilities are unavailable",
        __name__, type(_op_exc).__name__, _op_exc)
