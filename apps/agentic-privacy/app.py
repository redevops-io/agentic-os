"""agentic-privacy — the DSAR (data-subject / consumer request) automation module.

The on-brand answer to "how do we fulfil GDPR access/erasure + CCPA/CPRA delete &
opt-out requests across all our systems?" — an agent that, on a verified request,
**fans out across every personal-data store** and assembles an access export, executes
a cascading erasure, or records an opt-out — with a tamper-evident audit log and an
SLA dashboard (GDPR 30d / CCPA 45d).

Same module pattern as agentic-crm / lifecycle: FastAPI agent + MD3 dashboard over the
live cores, brain = DeepSeek-V4-Flash (drafts the human-readable export + response).

The DSAR engine — connectors (erpnext · listmonk · chatwoot · lago · umami · zitadel · s3),
the fan-out, the audit hash-chain, and the agent actions (intake · access · delete ·
retention) — lives in core.py (pure: stdlib + httpx, no web / context-runtime deps) so the
Mission Runtime operator can invoke it and it can be tested against fake cores. This module
imports config + actions from there and keeps the FastAPI surface + the optional LLM narration.

Endpoints:
  POST /request   {"type":"access|delete|correct|opt_out","email":"..."}
                  -> opens a PENDING request (same as /intake); body `verified`/`confirm` are ignored.
  GET  /verify?id=&token=   confirm page for the emailed link (NO state change: scanners can't burn the token)
  POST /verify    id+token (form or JSON) redeem the single-use token (the ONLY identity proof)
  GET  /export?id=&token=   single-use, short-TTL access export (when no mailer delivered it)
  POST /fulfil/{id}         operator Basic auth: execute a VERIFIED delete/correct (verify-after-act)
  GET  /          MD3 dashboard: open/closed requests, SLA timers, per-system coverage
  GET  /api/activity   the same as JSON   ·   GET /health

Identity is proven only by redeeming the emailed token; erasure/correction additionally need operator
confirmation (runtime approval gate or Basic auth) and are read back (re-find) before reporting success.
PRIVACY_DEV_MODE exposes the verify link; PRIVACY_DEMO_MODE runs the one-click flow on synthetic data only.
"""
from __future__ import annotations

import base64
import html
import json
import os
import secrets

# ── enterprise permissions plane — gate tool/data access by (app, user) ──
# Basic default without the enterprise package and without CR_PERMISSIONS / CR_IDENTITY_*; if either is set and the gate cannot
# be installed, agentic_os.app_gate refuses to start the app (it never runs ungated by accident).
from agentic_os.app_gate import RequestContext as _RequestContext, denial_result as _denial_result, deny_response as _deny_response, install_gate as _install_gate
from agentic_os.app_gate import with_authorization as _with_authorization, public_intake as _public_intake_ticket
_GATE_APP = "agentic-privacy"
_req_principal = _install_gate(_GATE_APP)   # starts gated or not at all when permissions were requested (RR-01)

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

# --- config + the pure DSAR engine (core.py — no web / context-runtime deps) --------
from . import core
from .core import (  # noqa: E402
    TENANT, SLA_DAYS, CONNECTORS, activity, handle_request, retention_scan, verify_audit_chain,
)

PORT = int(os.environ.get("PORT", "8212"))

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


app = FastAPI(title=f"agentic-privacy ({TENANT})")

from agentic_os.app_kit import route_authz as _route_authz  # noqa: E402
from .route_policy import APP as _APP, ROUTE_POLICIES as _ROUTE_POLICIES  # noqa: E402
_route_authz.install(app, _APP, _ROUTE_POLICIES)   # one enforcement layer for every write route
app.add_middleware(_RequestContext)   # the active gate (basic or enterprise) sees THIS request's principal in every route


# --- rate limiting (in-memory; per replica) -----------------------------------
# Per-client-IP token bucket on the public endpoints + a per-email hourly cap on /intake (stops mail-bombing a
# victim's address with verification emails). The client IP comes from the socket peer unless PRIVACY_TRUST_PROXY
# is true, in which case the first X-Forwarded-For hop is used (set it ONLY behind a proxy that overwrites it).
import threading as _threading
import time as _time
from collections import deque as _deque

RATE_BURST = 10


def _rate_per_min() -> float:
    try:
        return float(os.environ.get("PRIVACY_RATE_PER_MIN", "20"))
    except ValueError:
        return 20.0


def _intake_per_hour() -> int:
    try:
        return int(os.environ.get("PRIVACY_INTAKE_PER_EMAIL_HOUR", "5"))
    except ValueError:
        return 5


from collections import OrderedDict as _OrderedDict

_RL_LOCK = _threading.Lock()
_BUCKETS: "_OrderedDict[str, list]" = _OrderedDict()          # ip -> [tokens, last_ts]   (bounded: the OLDEST client is evicted, never everyone)
_EMAIL_HITS: "_OrderedDict[str, _deque[float]]" = _OrderedDict()
_GLOBAL_HITS: "_deque[float]" = _deque()                      # one window for every accepted public submission (a bound on the e-mails this app can be made to send per hour)
_RL_MAX_KEYS = 50_000


def _reset_rate_limits() -> None:
    with _RL_LOCK:
        _BUCKETS.clear()
        _EMAIL_HITS.clear()
        _GLOBAL_HITS.clear()


def _global_per_hour() -> int:
    try:
        return max(0, int(os.environ.get("PRIVACY_INTAKE_GLOBAL_PER_HOUR", "500")))
    except ValueError:
        return 500


def _global_cap(now: float | None = None) -> float:
    """Global cap on accepted public submissions per rolling hour. Returns 0.0 if allowed (and records the hit) else seconds to wait."""
    now = _time.monotonic() if now is None else now
    cap = _global_per_hour()
    with _RL_LOCK:
        while _GLOBAL_HITS and now - _GLOBAL_HITS[0] >= 3600:
            _GLOBAL_HITS.popleft()
        if len(_GLOBAL_HITS) >= cap:
            return 3600 - (now - _GLOBAL_HITS[0]) if _GLOBAL_HITS else 3600.0
        _GLOBAL_HITS.append(now)
        return 0.0


def _client_ip(request: Request) -> str:
    if core._truthy("PRIVACY_TRUST_PROXY"):
        xff = request.headers.get("x-forwarded-for", "")
        if xff.split(",")[0].strip():
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _take(key: str, now: float | None = None) -> float:
    """Take one token for `key`; return 0.0 if allowed else the seconds until a token is available."""
    now = _time.monotonic() if now is None else now
    rate = _rate_per_min() / 60.0
    with _RL_LOCK:
        tokens, last = _BUCKETS.get(key, [float(RATE_BURST), now])
        tokens = min(float(RATE_BURST), tokens + (now - last) * rate)
        allowed = tokens >= 1.0
        _BUCKETS[key] = [tokens - 1.0 if allowed else tokens, now]
        _BUCKETS.move_to_end(key)
        while len(_BUCKETS) > _RL_MAX_KEYS:
            _BUCKETS.popitem(last=False)
        if allowed:
            return 0.0
        return (1.0 - tokens) / rate if rate > 0 else 60.0


def _limited(retry_after: float, what: str = "rate limit exceeded") -> JSONResponse:
    secs = max(1, int(retry_after + 0.999))
    return JSONResponse({"status": "error", "error": what, "retry_after": secs}, status_code=429,
                        headers={"Retry-After": str(secs), "Cache-Control": "no-store"})


def _rate_check(request: Request) -> JSONResponse | None:
    wait = _take(_client_ip(request))
    return _limited(wait) if wait else None


def _email_cap(email: str, now: float | None = None) -> float:
    """Per-email intake cap per rolling hour. Returns 0.0 if allowed (and records the hit) else seconds to wait."""
    now = _time.monotonic() if now is None else now
    cap = _intake_per_hour()
    with _RL_LOCK:
        q = _EMAIL_HITS.setdefault(email, _deque())
        _EMAIL_HITS.move_to_end(email)
        while len(_EMAIL_HITS) > _RL_MAX_KEYS:
            _EMAIL_HITS.popitem(last=False)
        while q and now - q[0] >= 3600:
            q.popleft()
        if len(q) >= cap:
            return 3600 - (now - q[0])
        q.append(now)
        return 0.0
SUBTITLE = ("Data-subject / consumer requests (GDPR access & erasure, CCPA/CPRA delete & "
            "opt-out) fulfilled across every system — verified, audited, on the clock.")


# --- the brain (drafts the export / subject response) ------------------------
def _llm_text(prompt: str, max_tokens: int = 500) -> str | None:
    """Optional LLM narration blurb via the GOVERNED router (N7) — None if no compliant route / any error.

    The prompt it receives is built by core (counts + category labels ONLY, never record content or the
    subject's email). It is not wired into any route unless PRIVACY_LLM_SUMMARY=true (opt-in).

    Classified CUSTOMER_RESTRICTED (personal-data context) so it can only ever reach a verified in-boundary
    endpoint; fail-closed to the deterministic summary otherwise. No direct-HTTP / external fallback.
    """
    try:
        from agentic_os.app.llm import governed_text
        from agentic_os.governance.classification import DataClassification
    except Exception:  # noqa: BLE001
        return None
    return governed_text(prompt, classifications=(DataClassification.CUSTOMER_RESTRICTED,),
                         max_tokens=max_tokens, temperature=0.2)


if core._truthy("PRIVACY_LLM_SUMMARY"):
    core.SUMMARIZER = _llm_text


# --- dashboard ---------------------------------------------------------------
BASE_CSS = """
:root{--surface:#131316;--surface-container-low:#1b1b1f;--surface-container:#1f1f23;
--surface-container-highest:#353539;--on-surface:#e4e2e6;--on-surface-variant:#c7c5ca;
--on-surface-muted:#918f96;--outline-variant:#2f2f33;--primary:#4fd1c5;--on-primary-container:#a8f0e6;
--primary-container:#00504a;--success:#5bd98a;--success-container:#0f3d22;--warning:#f5b544;
--warning-container:#4a3500;--danger:#f2544f;--danger-container:#5c1512;--info:#5aa9f0;--info-container:#103a5c;
--sp-1:4px;--sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-5:24px;--radius-lg:16px;--radius-pill:999px;
--font-sans:"Roboto",system-ui,sans-serif;--font-mono:"Roboto Mono",ui-monospace,monospace;}
*{box-sizing:border-box}.page{background:var(--surface);color:var(--on-surface);font-family:var(--font-sans);padding:var(--sp-5);margin:0}
.shell{max-width:1280px;margin-inline:auto;display:flex;flex-direction:column;gap:var(--sp-5)}
.kpi-row{display:grid;gap:var(--sp-4);grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.card{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-4)}
.card__head{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3)}.card__title{font:500 16px/24px var(--font-sans);margin:0}
.tile{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-4) var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-1)}
.tile__label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--on-surface-muted)}
.tile__value{font:500 32px/40px var(--font-mono)}.tile__delta{font:500 12px/16px var(--font-sans);color:var(--on-surface-variant)}
.pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:var(--radius-pill);font:500 12px/1 var(--font-sans)}
.pill--success{background:var(--success-container);color:var(--success)}.pill--warn{background:var(--warning-container);color:var(--warning)}
.pill--danger{background:var(--danger-container);color:var(--danger)}.pill--info{background:var(--info-container);color:var(--info)}.pill--neutral{background:var(--surface-container-highest);color:var(--on-surface-variant)}.pill__dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.table{width:100%;border-collapse:collapse;font-size:14px}.table th{text-align:left;color:var(--on-surface-muted);font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;padding:var(--sp-3) var(--sp-4);border-bottom:1px solid var(--outline-variant)}
.table td{padding:var(--sp-3) var(--sp-4);border-bottom:1px solid var(--outline-variant)}.table tbody tr:hover{background:rgba(228,226,230,.08)}
.appbar{background:var(--surface-container-low);border:1px solid var(--outline-variant);border-radius:var(--radius-lg);padding:var(--sp-5)}
.appbar__row{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap}.appbar h1{margin:0;font:400 28px/36px var(--font-sans)}
.appbar__tenant{margin-top:var(--sp-3);color:var(--on-surface-variant);font:400 14px/20px}.appbar__tenant b{color:var(--on-surface)}
.appbar__sub{margin-top:var(--sp-2);color:var(--on-surface-muted);font:400 14px/20px;max-width:820px}.spacer{flex:1}
.section-label{font:500 12px/16px var(--font-sans);letter-spacing:.5px;text-transform:uppercase;color:var(--primary);margin:0}
.chips{display:flex;flex-wrap:wrap;gap:var(--sp-2)}.footer{color:var(--on-surface-muted);font:400 12px/16px;text-align:center;padding-top:var(--sp-2)}
"""
FONT = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=Roboto:wght@400;500&family=Roboto+Mono:wght@400;500&display=swap">')


def _esc(v) -> str:
    return html.escape(str(v))


def render(d: dict) -> str:
    kpis = "".join(f"<div class='tile'><div class='tile__label'>{_esc(k['label'])}</div>"
                   f"<div class='tile__value'>{_esc(k['value'])}</div>"
                   f"<div class='tile__delta'>{_esc(k['note'])}</div></div>" for k in d["kpis"])
    chips = "".join(f"<span class='pill {'pill--success' if c['configured'] else 'pill--neutral'}'>"
                    f"<span class='pill__dot'></span>{_esc(c['system'])}"
                    f"{' · live' if c['configured'] else ' · stub'}</span>" for c in d["coverage"])

    def spill(s):
        return {"fulfilled": "pill--success", "open": "pill--info",
                "awaiting_verification": "pill--warn"}.get(s, "pill--neutral")
    rows = "".join(
        "<tr>"
        f"<td style='font-family:var(--font-mono)'>{_esc(r.get('id'))}</td>"
        f"<td>{_esc(r.get('type'))}</td><td>{_esc(r.get('email'))}</td>"
        f"<td><span class='pill {spill(r.get('status'))}'>{_esc((r.get('status') or '').upper())}</span></td>"
        f"<td>{_esc((r.get('due_at') or '')[:10])}</td></tr>"
        for r in d["requests"]) or "<tr><td colspan=5>No requests yet. POST /request to file one.</td></tr>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Agentic Privacy — {_esc(d['tenant'])}</title>
{FONT}<style>{BASE_CSS}</style></head><body class="page"><div class="shell">
<header class="appbar"><div class="appbar__row"><h1>Agentic Privacy</h1>
<span class="pill pill--info"><span class="pill__dot"></span>DSAR orchestrator</span><span class="spacer"></span></div>
<div class="appbar__tenant"><b>{_esc(d['tenant'])}</b> · GDPR / CCPA / CPRA data-subject requests</div>
<div class="appbar__sub">{_esc(SUBTITLE)}</div></header>
<section class='kpi-row'>{kpis}</section>
<section class='shell' style='gap:var(--sp-4)'><div class='section-label'>System coverage</div>
<div class='card'><div class='chips'>{chips}</div></div>
<div class='section-label'>Requests &amp; SLA</div>
<div class='card'><table class='table'><thead><tr><th>Request</th><th>Type</th><th>Subject</th><th>Status</th><th>Due</th></tr></thead>
<tbody>{rows}</tbody></table></div></section>
<footer class="footer">agentic-privacy · access · erasure · opt-out · audited · redevops.io Agentic Business OS</footer>
</div></body></html>"""


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return _with_authorization(_GATE_APP, {"status": "ok", "connectors_live": [s for s, (_f, _d, cfg) in CONNECTORS.items() if cfg()]})


@app.get("/api/activity")
def api_activity() -> JSONResponse:
    if (_r := _deny_response(_GATE_APP, "privacy_summary")):
        return _r
    return JSONResponse(activity())


# ──────── conversational agent · Context Runtime + redevops-rag + agent-harness ────────
try:
    from context_runtime.integrations.agent_console import AgentConsole as _AgentConsole, tool as _crtool
except Exception:  # noqa: BLE001
    _AgentConsole = None

_PRIVACY_PRIMER = """A privacy REQUEST is a data subject exercising a right: ACCESS (get a copy of their data), DELETE (erasure), CORRECT (fix inaccurate data), or OPT-OUT (of sale/sharing — the CCPA/CPRA "Do Not Sell or Share" right). Anyone files one on the public /intake form.

VERIFICATION comes first: a request is PENDING until the person's identity is confirmed. We never return or delete anyone's data on an unverified request. An operator (or an emailed link) promotes it to fulfilment.

The SLA is the legal clock to respond — typically 30 days (GDPR) or 45 days (CCPA/CPRA). Overdue requests are the ones past that due date.

The DATA MAP (coverage) is every personal-data system we can reach: ERPNext (customers/leads), Listmonk (subscribers), Chatwoot (contacts), Lago (billing), Umami (analytics), Zitadel (identity). A connector is "live" once its credentials are configured. Access and delete fan out across all live connectors.

Every action is written to a tamper-evident AUDIT log (a hash chain): each entry commits to the previous one, so the record can't be altered after the fact. That's your proof of compliance.

To fulfil a verified access request the agent gathers records from every connector; to fulfil a delete it removes the subject from each — both only after verification, and both audited."""


def _t_privacy_summary(_a: dict) -> dict:
    d = activity()
    k = {x["label"]: x["value"] for x in d["kpis"]}
    return {"text": f"{k.get('Open requests')} open requests · {k.get('Overdue (SLA)')} overdue · "
            f"connectors live {k.get('Connectors live')} · {k.get('Fulfilled')} fulfilled.", "data": d["kpis"]}


def _t_list_requests(_a: dict) -> dict:
    reqs = activity()["requests"]
    if not reqs:
        return {"text": "No privacy requests on file.", "data": []}
    lines = [f"{r.get('type', '?')} · {r.get('status', '?')} · {r.get('email', '—')}"
             + (f" · due {r['due_at'][:10]}" if r.get("due_at") else "") for r in reqs[:12]]
    return {"text": "\n".join(lines), "data": reqs}


def _t_data_map(_a: dict) -> dict:
    cov = activity()["coverage"]
    lines = [f"{c['system']}: {'live' if c['configured'] else 'not configured'}" for c in cov]
    return {"text": "Personal-data systems:\n" + "\n".join(lines), "data": cov}


class _GovernedConsoleModel:
    """The conversational console's model object: every ``complete`` goes through ``governed_text`` with an
    explicit classification. Raises when there is no compliant route so the console drops to its
    deterministic keyword/primer fallbacks (it never reaches a model on its own)."""

    def complete(self, req):
        from types import SimpleNamespace
        from agentic_os.app.llm import governed_text
        from agentic_os.governance.classification import DataClassification
        prompt = "\n\n".join(str(m.get("content", "")) for m in req.messages)
        txt = governed_text(prompt, classifications=(DataClassification.CUSTOMER_RESTRICTED,), system=req.system or "",
                            max_tokens=int(req.max_tokens or 600), temperature=0.2)
        if not txt:
            raise RuntimeError("no compliant in-boundary model route")
        return SimpleNamespace(text=txt, model="in-boundary", tier="in_boundary", prompt_tokens=0,
                               completion_tokens=0, est_cost_usd=0.0, cache_hit=False, models_used=())

    def info(self):
        from types import SimpleNamespace
        return SimpleNamespace(name="governed-in-boundary")


if _AgentConsole is not None:
    _PRIVACY_CONSOLE = _AgentConsole(
        f"{TENANT} Privacy", _PRIVACY_PRIMER,
        tools=[
            _crtool("privacy_summary", "open/overdue/fulfilled privacy requests and connectors live", _t_privacy_summary),
            _crtool("list_requests", "the privacy requests on file: type, status, email, due date", _t_list_requests),
            _crtool("data_map", "which personal-data systems (connectors) are live", _t_data_map),
        ],
        suggestions=["Any overdue requests?", "Show open requests", "How do I verify a requester?", "What is the DSAR SLA?"],
        subtitle="Ask about DSAR requests, the data map, verification and SLAs.",
        model=_GovernedConsoleModel(),
        allow_side_effects=[],
    )
else:
    _PRIVACY_CONSOLE = None


@app.post("/api/agent")
async def api_agent(request: Request) -> JSONResponse:
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        body = {}
    if _PRIVACY_CONSOLE is None:
        return JSONResponse({"intent": "help", "text": "The assistant is offline (context-runtime unavailable).", "evidence": []})
    return JSONResponse(_PRIVACY_CONSOLE.respond((body or {}).get("message", ""), principal=_req_principal(request)))


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    if (_r := _deny_response(_GATE_APP, "privacy_summary")):
        return _r
    page = render(activity())
    if _PRIVACY_CONSOLE is not None:
        panel = "<section class='shell' style='margin-top:16px'>" + _PRIVACY_CONSOLE.panel_html("privacy") + "</section>"
        page = page.replace("<footer", panel + "<footer", 1)
    return page


def _operator_credential() -> tuple[str, str]:
    """Operator Basic credential. OPERATOR_BASIC_AUTH_USER/_PASS (or a single OPERATOR_BASIC_AUTH / OUTREACH_BASIC_AUTH
    "user:pass"). Unset => ("","") => every operator route fails closed."""
    user = os.environ.get("OPERATOR_BASIC_AUTH_USER", "")
    pw = os.environ.get("OPERATOR_BASIC_AUTH_PASS", "")
    if not (user and pw):
        combo = os.environ.get("OPERATOR_BASIC_AUTH") or os.environ.get("OUTREACH_BASIC_AUTH") or ""
        user, _, pw = combo.partition(":")
    return user, pw


def _operator_ok(request: Request) -> bool:
    user, pw = _operator_credential()
    header = request.headers.get("authorization", "")
    if not (user and pw) or not header.startswith("Basic "):
        return False
    try:
        got_user, _, got_pw = base64.b64decode(header[6:]).decode("utf-8", "replace").partition(":")
    except Exception:  # noqa: BLE001
        return False
    return secrets.compare_digest(got_user, user) and secrets.compare_digest(got_pw, pw)


def require_operator(request: Request) -> None:
    """503 when no operator credential is configured (fail closed), 401 when missing/invalid."""
    if not all(_operator_credential()):
        raise HTTPException(503, "operator confirmation is not configured (set OPERATOR_BASIC_AUTH_USER/_PASS)")
    if not _operator_ok(request):
        raise HTTPException(401, "operator authentication required",
                            headers={"WWW-Authenticate": 'Basic realm="agentic-privacy operator"'})


def _public(resp: dict) -> dict:
    """Public responses never carry the verification link outside PRIVACY_DEV_MODE (defence in depth)."""
    if not core.DEV_MODE:
        resp.pop("verify_url", None)
        resp.pop("dev_note", None)
    return resp


_PUBLIC_KEYS = {"email", "type", "website"}                  # the whole schema of a public submission; "website" is the honeypot (a hidden field a person never fills)


def _max_body() -> int:
    try:
        return max(64, int(os.environ.get("PRIVACY_INTAKE_MAX_BODY", "2048")))
    except ValueError:
        return 2048


def _public_error(status: int, error: str) -> JSONResponse:
    return JSONResponse({"status": "error", "error": error}, status_code=status, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


async def _read_public_submission(request: Request):
    """Strict schema for a public submission: JSON, at most ``PRIVACY_INTAKE_MAX_BODY`` bytes (default 2048, enforced while reading), an object with ONLY ``email`` (a string), ``type`` (a string, optional)
    and the honeypot ``website`` (a string, optional). Returns (submission, None) or (None, the error response)."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return None, _public_error(415, "send application/json")
    limit = _max_body()
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None, _public_error(413, "request too large")
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > limit:
            return None, _public_error(413, "request too large")
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None, _public_error(400, "the body must be a JSON object")
    if not isinstance(body, dict) or set(body) - _PUBLIC_KEYS or "email" not in body:
        return None, _public_error(400, "the body must be a JSON object with an email and optionally a type")
    email, rtype, trap = body.get("email"), body.get("type", "access"), body.get("website", "")
    if not (isinstance(email, str) and isinstance(rtype, str) and isinstance(trap, str)):
        return None, _public_error(400, "email, type and website must be strings")
    email = email.lower()                                         # (no strip: padding is not a mailbox)
    if not core.valid_public_email(email) or core.PUBLIC_REQUEST_TYPES.get(rtype) is None:
        return None, _public_error(400, "a valid email address and a known request type are required")
    return {"email": email, "type": core.PUBLIC_REQUEST_TYPES[rtype], "trap": trap}, None


def _public_headers(resp: JSONResponse) -> JSONResponse:
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


async def _public_submit(request: Request, path: str) -> JSONResponse:
    """The body of POST /intake and POST /request in the normal mode. NO grant is consulted (a data subject must be able to reach it): this is the registered public-intake exception, which
    can only open a PENDING request and send one verification e-mail (see core.public_intake and agentic_os.tool_actions.PUBLIC_INTAKE_ROUTES)."""
    if (limited := _rate_check(request)) is not None:
        return limited
    sub, err = await _read_public_submission(request)
    if err is not None:
        return err
    if sub["trap"].strip():                                           # a bot: the same answer as a person, and nothing is created or sent
        ch = "postmark" if core.POSTMARK_TOKEN else "smtp" if core.SMTP_HOST else "undeliverable"
        return _public_headers(JSONResponse({"status": "received", "request_id": core._new_dsar_id(), "type": sub["type"], "delivery": ch, "message": core.public_intake_message(ch)}))
    generic = "too many requests; try again later"
    if (wait := _email_cap(sub["email"])):
        return _limited(wait, generic)
    if (wait := _global_cap()):
        return _limited(wait, generic)
    ticket = (_public_intake_ticket(_GATE_APP, "POST", "/intake") if path == "/intake" else _public_intake_ticket(_GATE_APP, "POST", "/request"))
    try:
        return _public_headers(JSONResponse(_public(core.public_intake(ticket, sub["email"], sub["type"], str(request.base_url)))))
    except core.PublicIntakeBusy:
        return _public_error(503, "temporarily unable to accept requests; try again later")


@app.post("/request")
async def request_route(request: Request) -> JSONResponse:
    if core.DEMO_MODE:                                            # the synthetic one-click demo store only (validate_demo_mode): the old, lenient flow
        if (limited := _rate_check(request)) is not None:
            return limited
        try:
            body = _strip_approval(await request.json())
        except Exception:
            body = {}
        return JSONResponse(_public(handle_request(body or {}, base_url=str(request.base_url))))
    return await _public_submit(request, "/request")


# --- public intake form ------------------------------------------------------
# A request filed here is PENDING (awaiting identity verification) — it never returns
# anyone's data. An operator (or, in production, an emailed verification link) promotes
# it to fulfilment via POST /request with verified=true. This is the page the sites'
# "Do Not Sell or Share" / "Privacy request" links point at.
INTAKE_HTML = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Privacy request — {html.escape(TENANT)}</title>
{FONT}<style>{BASE_CSS}
.form{{max-width:560px;margin:8vh auto;display:flex;flex-direction:column;gap:var(--sp-4)}}
input,select,button{{font:400 15px/1.4 var(--font-sans);padding:12px 14px;border-radius:12px;border:1px solid var(--outline-variant);background:var(--surface-container);color:var(--on-surface)}}
button{{background:var(--primary-container);color:var(--on-primary-container);border:none;cursor:pointer;font-weight:500}}
label{{font:500 13px/1.4 var(--font-sans);color:var(--on-surface-variant)}}
#msg{{margin-top:var(--sp-3);font-size:14px}}</style></head>
<body class="page"><div class="form">
<h1 style="margin:0;font:400 28px/36px var(--font-sans)">Submit a privacy request</h1>
<p class="appbar__sub" style="margin:0">{html.escape(TENANT)} — exercise your GDPR / CCPA / CPRA rights. We verify your
identity before acting and respond within {SLA_DAYS} days.</p>
<label>Your email (the subject of the request)</label>
<input id="email" type="email" placeholder="you@example.com" required maxlength="254">
<input id="website" name="website" type="text" tabindex="-1" autocomplete="off" aria-hidden="true" style="position:absolute;left:-10000px;width:1px;height:1px;opacity:0">
<label>Request type</label>
<select id="type">
  <option value="access">Access — what data do you hold about me?</option>
  <option value="delete">Delete / erase my personal data</option>
  <option value="opt_out">Do Not Sell or Share my personal information</option>
  <option value="correct">Correct my data</option>
</select>
<button onclick="submitReq()">Submit request</button>
<div id="msg"></div>
</div>
<script>
async function submitReq() {{
  const email = document.getElementById('email').value.trim();
  const type = document.getElementById('type').value;
  const msg = document.getElementById('msg');
  if (!email) {{ msg.textContent = 'Please enter your email.'; return; }}
  msg.textContent = 'Submitting…';
  try {{
    const r = await fetch('/intake', {{method:'POST',headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{email, type, website: document.getElementById('website').value}})}});
    const d = await r.json();
    msg.style.color = '#5bd98a';
    msg.textContent = d.message || ('Received: ' + (d.request_id||''));
  }} catch (e) {{ msg.style.color = '#f2544f'; msg.textContent = 'Something went wrong — email privacy@ instead.'; }}
}}
</script></body></html>"""


@app.get("/intake", response_class=HTMLResponse)
def intake_form() -> str:
    return INTAKE_HTML


@app.post("/intake")
async def intake_submit(request: Request) -> JSONResponse:
    return await _public_submit(request, "/intake")


def _verify_page(message: str, ok: bool, raw: bool = False) -> str:
    color = "#5bd98a" if ok else "#f2544f"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>Privacy request — {html.escape(TENANT)}</title>{FONT}<style>{BASE_CSS}</style></head>"
            f"<body class='page'><div class='shell' style='max-width:560px;margin:12vh auto'>"
            f"<div class='card'><h1 style='margin:0;font:400 24px/32px var(--font-sans)'>Privacy request</h1>"
            f"<p style='color:{color};font-size:15px'>{message if raw else html.escape(message)}</p>"
            f"<p class='footer' style='text-align:left'>{html.escape(TENANT)} · GDPR / CCPA / CPRA</p>"
            f"</div></div></body></html>")


def _do_verify(id: str, token: str) -> str:
    res = core.verify_request(id, token)
    if not res.get("ok"):
        return _verify_page("This verification link is invalid, already used, or expired.", False)
    msg = html.escape(res.get("message") or "Identity verified.")
    if res.get("export_url"):
        msg += f' <a href="{html.escape(res["export_url"])}">Download your export (single use)</a>'
        return _verify_page(msg, True, raw=True)
    return _verify_page(msg, True)


_PAGE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _confirm_page(id: str, token: str) -> str:
    """GET /verify: a confirm page ONLY — no state change, so an email link-scanner / prefetcher cannot burn the
    single-use token. The human clicks the button, which POSTs the id+token (hidden fields, unguessable secret =
    CSRF-safe: a cross-site form cannot know the token)."""
    form = ("<form method='post' action='/verify' style='margin:0'>"
            f"<input type='hidden' name='id' value='{html.escape(id)}'>"
            f"<input type='hidden' name='token' value='{html.escape(token)}'>"
            "<button type='submit' style='padding:12px 18px;border-radius:12px;border:none;cursor:pointer;"
            "background:var(--primary-container);color:var(--on-primary-container);font:500 15px var(--font-sans)'>"
            "Confirm this is my request</button></form>")
    return _verify_page("Please confirm that you made this privacy request." + "</p>" + form + "<p>", True, raw=True)


@app.get("/verify")
def verify(request: Request, id: str = "", token: str = "") -> HTMLResponse:
    """Show the confirm page for the emailed link. Does NOT consume the token (see POST /verify)."""
    if (limited := _rate_check(request)) is not None:
        return limited
    return HTMLResponse(_confirm_page(id, token), headers=_PAGE_HEADERS)


@app.post("/verify")
async def verify_post(request: Request) -> HTMLResponse:
    """Redeem the emailed single-use token (request id + token). The only way identity is proven.
    Accepts the confirm-page form (application/x-www-form-urlencoded) or JSON {"id","token"}."""
    if (limited := _rate_check(request)) is not None:
        return limited
    raw = await request.body()
    body: dict = {}
    if "json" in request.headers.get("content-type", "").lower():
        try:
            body = _strip_approval(__import__("json").loads(raw or b"{}"))
        except Exception:  # noqa: BLE001
            body = {}
    else:
        from urllib.parse import parse_qs
        body = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items() if v}
    if not isinstance(body, dict):
        body = {}
    return HTMLResponse(_do_verify(str(body.get("id", "")), str(body.get("token", ""))), headers=_PAGE_HEADERS)


@app.get("/export")
def export(request: Request, id: str = "", token: str = "") -> JSONResponse:
    """Single-use, short-TTL access export, protected by request id + export token (issued only after /verify)."""
    if (limited := _rate_check(request)) is not None:
        return limited
    res = core.redeem_export(id, token)
    if not res.get("ok"):
        return JSONResponse({"status": "error", "error": "invalid, used, or expired"}, status_code=403,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(res, headers={"Cache-Control": "no-store"})


@app.post("/fulfil/{request_id}")
async def fulfil_route(request_id: str, request: Request) -> JSONResponse:
    """Operator confirmation of a VERIFIED delete/correct request (Basic auth; fail-closed)."""
    if (_r := _deny_response(_GATE_APP, "privacy.delete")):
        return _r
    require_operator(request)
    try:
        body = _strip_approval(await request.json())
    except Exception:  # noqa: BLE001
        body = {}
    res = core.fulfil(request_id, (body or {}).get("email"), (body or {}).get("corrections"),
                      actor="operator-basic-auth")
    return JSONResponse(res, status_code=409 if res.get("status") == "refused" else 200)


@app.get("/audit/verify")
def audit_verify() -> JSONResponse:
    """Confirm the audit log's hash chain is intact (tamper-evidence)."""
    if (_r := _deny_response(_GATE_APP, "privacy_summary")):
        return _r
    return JSONResponse(verify_audit_chain())


@app.post("/retention")
async def retention_route(request: Request) -> JSONResponse:
    """Retention enforcement: scan (dry-run) or purge (confirm:true) PII past the window."""
    if (_r := _deny_response(_GATE_APP, "privacy.retention")):
        return _r
    try:
        body = _strip_approval(await request.json())
    except Exception:
        body = {}
    want_purge = bool((body or {}).get("confirm"))
    if want_purge and (_r := _deny_response(_GATE_APP, "privacy.delete")):          # a PURGE removes data: it needs the delete grant too
        return _r
    # Unauthenticated callers (and any call without confirm:true) get a counts-only DRY RUN. A real purge needs
    # operator Basic auth AND confirm:true.
    if want_purge and _operator_ok(request):
        return JSONResponse(retention_scan(confirm=True))
    res = retention_scan(confirm=False)
    if want_purge:
        res["note"] = "DRY RUN — purge requires operator authentication."
    return JSONResponse(res)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    from agentic_os.app_gate import require_explicit_setting_when_exposed as _exposed
    _exposed("0.0.0.0")   # a reachable bind needs an explicit permissions setting
    uvicorn.run(app, host="0.0.0.0", port=PORT)


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive DSAR fulfilment as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_privacy_operator
    app.include_router(build_privacy_operator().router())
    # Runtime-native: register this app's governed contract at boot (guarded with the mount above).
    from agentic_os.app_kit.boot import register_app_manifest as _register_runtime_native
    from . import manifest as _runtime_native_manifest
    _register_runtime_native(_runtime_native_manifest)
except Exception as _op_exc:  # agentic_os absent / operator build failed — surface it, do not hide it
    import logging as _logging
    _logging.getLogger("agentic_os.app").error(
        "%s: operator/capability surface NOT mounted (%s: %s) — /invoke + /capabilities are unavailable",
        __name__, type(_op_exc).__name__, _op_exc)
