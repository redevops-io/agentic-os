"""agentic-crm core — the pure ERPNext-CRM REST client + agentic actions.

No web framework, no context-runtime: just stdlib + httpx against a real ERPNext CRM
core (Lead / Opportunity / Customer doctypes). This is the layer the FastAPI app renders
from AND the Mission Runtime operator invokes, so the capability handlers can be tested
against a fake ERPNext without booting the whole console.

The LLM only NARRATES here — every action is deterministic ERPNext REST work. The brain
lives in `app.py` (context-runtime) and is injected as an optional `llm` callback
(`llm(prompt, max_tokens) -> str | None`); the actions all work with `llm=None`.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Callable

import httpx

# ── config (env; seed.py writes agents/agentic-crm/.env) ─────────────────────
# Idempotent .env load so this module is self-sufficient when imported by the operator
# (i.e. without the FastAPI app having run its own loader first).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

ERPNEXT_URL = os.environ.get("ERPNEXT_URL", "http://localhost:8092").rstrip("/")
ERPNEXT_API_KEY = os.environ.get("ERPNEXT_API_KEY", "")
ERPNEXT_API_SECRET = os.environ.get("ERPNEXT_API_SECRET", "")
ERPNEXT_FRONT_URL = os.environ.get("ERPNEXT_FRONT_URL", "http://localhost:8092").rstrip("/")

TENANT = os.environ.get("CRM_TENANT", "Meridian Wealth Management")
SUBTITLE = ("Sales pipeline that scores, researches, drafts outreach, and answers questions "
            "on a real ERPNext CRM core — a human sends any outreach before it reaches a prospect.")

# `llm(prompt, max_tokens) -> str | None` — the optional narration callback.
LLM = Callable[..., "str | None"]


# ── ERPNext REST client ──────────────────────────────────────────────────────
def _headers() -> dict:
    return {"Authorization": f"token {ERPNEXT_API_KEY}:{ERPNEXT_API_SECRET}",
            "Content-Type": "application/json"}


def erp_connected() -> bool:
    try:
        r = httpx.get(f"{ERPNEXT_URL}/api/resource/Lead", headers=_headers(),
                      params={"limit_page_length": 1}, timeout=4.0)
        return r.status_code == 200
    except Exception:
        return False


def _list(doctype: str, fields: list[str], filters: list | None = None) -> list[dict]:
    """List a doctype with the given fields (all rows; ERPNext caps at limit_page_length=0)."""
    params = {"fields": json.dumps(fields), "limit_page_length": 0}
    if filters:
        params["filters"] = json.dumps(filters)
    path = "/api/resource/" + urllib.parse.quote(doctype)
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(ERPNEXT_URL + path, headers=_headers(), params=params)
            r.raise_for_status()
            return r.json().get("data", [])
    except Exception:
        # A single invalid fieldname 417s the whole query (ERPNext field names vary
        # by version). Degrade to name-only so counts never silently read as zero.
        if fields != ["name"]:
            return _list(doctype, ["name"], filters)
        return []


def _get_doc(doctype: str, name: str) -> dict | None:
    path = "/api/resource/" + urllib.parse.quote(doctype) + "/" + urllib.parse.quote(name)
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(ERPNEXT_URL + path, headers=_headers())
            if r.status_code == 200:
                return r.json().get("data")
    except Exception:
        pass
    return None


def _add_comment(doctype: str, name: str, text: str) -> bool:
    """Attach an auditable Comment to a CRM record (where agent output lands)."""
    body = {"reference_doctype": doctype, "reference_name": name,
            "content": text, "comment_type": "Comment", "comment_email": "agentic-crm",
            "comment_by": "agentic-crm"}
    try:
        with httpx.Client(timeout=12.0) as client:
            r = client.post(ERPNEXT_URL + "/api/resource/Comment", headers=_headers(),
                            json=body)
            return r.status_code in (200, 201)
    except Exception:
        return False


def _set_field(doctype: str, name: str, field: str, value) -> bool:
    path = "/api/resource/" + urllib.parse.quote(doctype) + "/" + urllib.parse.quote(name)
    try:
        with httpx.Client(timeout=12.0) as client:
            r = client.put(ERPNEXT_URL + path, headers=_headers(), json={field: value})
            return r.status_code in (200, 201)
    except Exception:
        return False


# ── live pipeline data + KPIs (cached briefly) ───────────────────────────────
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 15.0


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_activity(force: bool = False) -> dict:
    """Pull REAL ERPNext CRM data and compute the pipeline KPIs the dashboard renders."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    connected = erp_connected()
    leads = _list("Lead", ["name", "lead_name", "company_name", "status",
                           "email_id", "creation"]) if connected else []
    opps = _list("Opportunity", ["name", "party_name", "customer_name", "status",
                                 "opportunity_amount", "sales_stage", "creation"]) if connected else []
    customers = _list("Customer", ["name"]) if connected else []

    # Lead funnel by status
    lead_status: dict[str, int] = {}
    for l in leads:
        lead_status[l.get("status") or "Open"] = lead_status.get(l.get("status") or "Open", 0) + 1

    # Opportunity pipeline by stage + value
    open_opps = [o for o in opps if (o.get("status") or "") not in ("Closed", "Lost", "Converted")]
    pipeline_value = round(sum(_f(o.get("opportunity_amount")) for o in open_opps), 2)
    stage_val: dict[str, float] = {}
    for o in open_opps:
        st = o.get("sales_stage") or "Prospecting"
        stage_val[st] = stage_val.get(st, 0.0) + _f(o.get("opportunity_amount"))
    won = sum(1 for o in opps if (o.get("status") or "") in ("Converted", "Closed"))
    lost = sum(1 for o in opps if (o.get("status") or "") == "Lost")
    win_rate = round(100 * won / (won + lost)) if (won + lost) else 0

    stage_total = sum(stage_val.values()) or 1.0
    stages = [{"label": k, "pct": int(round(100 * v / stage_total)), "value": round(v, 0)}
              for k, v in sorted(stage_val.items(), key=lambda kv: kv[1], reverse=True)]

    # Lead queue: open leads, newest first — the agent's work list
    open_leads = sorted([l for l in leads if (l.get("status") or "Open") in ("Open", "Lead", "Replied")],
                        key=lambda l: l.get("creation") or "", reverse=True)
    queue = [{"name": l.get("name"), "lead": l.get("lead_name") or l.get("company_name") or "—",
              "company": l.get("company_name") or "—", "status": l.get("status") or "Open",
              "source": l.get("source") or "—", "email": l.get("email_id") or "—"}
             for l in open_leads[:25]]

    data = {
        "tenant": TENANT, "core": "erpnext-crm", "connected": connected,
        "front_url": ERPNEXT_FRONT_URL,
        "kpis": [
            {"label": "Open leads", "value": str(sum(lead_status.get(s, 0) for s in ("Open", "Lead", "Replied"))),
             "note": f"{len(leads)} total leads"},
            {"label": "Open pipeline", "value": f"${pipeline_value:,.0f}",
             "note": f"{len(open_opps)} open opportunities"},
            {"label": "Win rate", "value": f"{win_rate}%", "note": f"{won} won · {lost} lost"},
            {"label": "Customers", "value": str(len(customers)), "note": "converted accounts"},
        ],
        "stages": stages, "queue": queue,
        "counts": {"leads": len(leads), "opps": len(opps), "customers": len(customers)},
    }
    _CACHE.update(ts=now, data=data)
    return data


# ── LLM-context builders (pure string work over known lead fields) ────────────
def _lead_brief(lead: dict) -> str:
    return (f"Lead: {lead.get('lead_name') or '—'} | Company: {lead.get('company_name') or '—'} | "
            f"Status: {lead.get('status') or '—'} | Source: {lead.get('source') or '—'} | "
            f"Email: {lead.get('email_id') or '—'} | Title: {lead.get('designation') or '—'} | "
            f"Industry: {lead.get('industry') or '—'} | Territory: {lead.get('territory') or '—'} | "
            f"Notes: {(lead.get('notes') or lead.get('lead_owner') or '')[:300]}")


def _research_signals(lead: dict) -> str:
    """Pull buying-signal / firmographic context. Uses DEERFLOW_URL when set (real web
    research); otherwise reasons over the known lead fields. Returns a short context block."""
    deer = os.environ.get("DEERFLOW_URL")
    company = lead.get("company_name") or lead.get("lead_name") or ""
    if deer and company:
        try:
            r = httpx.post(deer.rstrip("/") + "/api/research",
                           json={"query": f"firmographics + recent buying signals (funding, hiring, "
                                          f"product launches, leadership changes) for {company}"},
                           timeout=60.0)
            if r.status_code == 200:
                return (r.json().get("summary") or "")[:1200]
        except Exception:
            pass
    return ""  # no external signals — the LLM reasons over known fields only


# ── agentic actions (deterministic ERPNext REST work; LLM only narrates) ──────
def score_lead(body: dict, llm: LLM | None = None) -> dict:
    """LLM-score a lead 0-100 + rationale, written back as a Lead Comment + lead_score.

    Side-effecting (writes to ERPNext). `llm` is the optional narration callback; with
    `llm=None` the action still runs — it posts the (un-scored) audit comment.
    """
    name = body.get("lead")
    lead = _get_doc("Lead", name) if name else None
    if not lead:
        return {"status": "error", "error": f"lead {name!r} not found", "action": "score_lead"}
    out = llm(
        "You are a B2B sales SDR agent. Score this lead 0-100 for fit + likelihood to convert, "
        "and give a 2-3 sentence rationale and the single best next action. "
        "Return STRICT JSON: {\"score\":<int>,\"rationale\":\"...\",\"next_action\":\"...\"}.\n\n"
        + _lead_brief(lead)) if llm else None
    parsed = {}
    if out:
        try:
            parsed = json.loads(out[out.find("{"): out.rfind("}") + 1])
        except Exception:
            parsed = {"score": None, "rationale": out[:500], "next_action": ""}
    note = (f"🤖 AGENT LEAD SCORE: {parsed.get('score')}/100\n"
            f"Rationale: {parsed.get('rationale','')}\n"
            f"Next action: {parsed.get('next_action','')}\n— agentic-crm (DeepSeek-V4-Flash)")
    posted = _add_comment("Lead", name, note)
    if parsed.get("score") is not None:
        _set_field("Lead", name, "lead_score", parsed.get("score"))  # ERPNext stock field
    return {"status": "done" if posted else "partial", "action": "score_lead", "lead": name,
            "score": parsed.get("score"), "rationale": parsed.get("rationale"),
            "next_action": parsed.get("next_action"), "written_to": "Lead comment + lead_score"}


def research(body: dict, llm: LLM | None = None) -> dict:
    """LLM enrichment brief (optionally web-enriched via DEERFLOW), saved as a Lead Comment.

    Side-effecting (writes a Comment to ERPNext).
    """
    name = body.get("lead")
    lead = _get_doc("Lead", name) if name else None
    if not lead:
        return {"status": "error", "error": f"lead {name!r} not found", "action": "research"}
    signals = _research_signals(lead)
    out = llm(
        "You are a B2B research agent. Write a tight enrichment brief for this lead: likely "
        "company size/industry, what they probably care about, and 2-3 plausible buying signals "
        "to look for. Keep it factual and hedge what you don't know.\n\n"
        + _lead_brief(lead) + (f"\n\nWeb signals:\n{signals}" if signals else ""),
        420) if llm else None
    brief = out or "(brain unavailable)"
    note = f"🤖 AGENT RESEARCH BRIEF:\n{brief}\n— agentic-crm" + (" [web-enriched]" if signals else " [from known fields]")
    posted = _add_comment("Lead", name, note)
    return {"status": "done" if posted else "partial", "action": "research", "lead": name,
            "brief": brief, "web_enriched": bool(signals)}


def draft_outreach(body: dict, llm: LLM | None = None) -> dict:
    """Draft a personalised first-touch email, saved as a Lead Comment for human review.

    DRAFTS ONLY — never auto-emails the prospect; a human sends it. Side-effecting (writes a
    Comment) but NOT approval-gated: nothing reaches a prospect from this action.
    """
    name = body.get("lead")
    lead = _get_doc("Lead", name) if name else None
    if not lead:
        return {"status": "error", "error": f"lead {name!r} not found", "action": "draft_outreach"}
    draft = llm(
        f"You are an SDR for {TENANT}. Write a concise, personalised first-touch outreach email "
        "(<=120 words) to this lead. One clear value hook + one soft call to action. No fake "
        "specifics, no pricing. Return ONLY subject + body.\n\n" + _lead_brief(lead),
        300) if llm else None
    draft = draft or "(brain unavailable)"
    note = ("🤖 AGENT OUTREACH DRAFT (review before sending — never auto-sent):\n\n"
            f"{draft}\n— agentic-crm")
    posted = _add_comment("Lead", name, note)
    return {"status": "done" if posted else "partial", "action": "draft_outreach", "lead": name,
            "draft": draft, "posted_as": "Lead comment",
            "requires": "human approval to send"}


def qualify(body: dict) -> dict:
    """Advance a Lead's status (pipeline progression). Side-effecting (writes the status field)."""
    name = body.get("lead")
    new_status = body.get("status", "Replied")
    lead = _get_doc("Lead", name) if name else None
    if not lead:
        return {"status": "error", "error": f"lead {name!r} not found", "action": "qualify"}
    ok = _set_field("Lead", name, "status", new_status)
    _add_comment("Lead", name, f"🤖 agentic-crm advanced status → {new_status}")
    return {"status": "done" if ok else "error", "action": "qualify", "lead": name,
            "new_status": new_status if ok else lead.get("status")}


def ask(body: dict, llm: LLM | None = None) -> dict:
    """NL-to-CRM: answer a question over the live pipeline snapshot. Read-only."""
    q = body.get("q", "").strip()
    if not q:
        return {"status": "error", "error": "q (question) required", "action": "ask"}
    data = fetch_activity()
    ctx = {"kpis": data["kpis"], "stages": data["stages"],
           "queue": data["queue"][:15], "counts": data["counts"]}
    out = llm(
        "You are a CRM analyst. Answer the question using ONLY this live pipeline snapshot "
        "(ERPNext CRM). Be concise and cite numbers. If it's not answerable from the snapshot, "
        f"say so.\n\nSNAPSHOT:\n{json.dumps(ctx)[:3500]}\n\nQUESTION: {q}", 400) if llm else None
    return {"status": "done", "action": "ask", "q": q, "answer": out or "(brain unavailable)"}
