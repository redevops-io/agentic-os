"""agentic-outreach-engine — agent layer + MD3 dashboard over a real Twenty CRM core.

A conversational agent layer + MD3 dashboard over a real Twenty CRM: discover leads, draft follow-ups,
summarize the pipeline, and explain the CRM — every action shows its work. Generic across workspaces
(configure OUTREACH_TENANT / OUTREACH_PITCH / OUTREACH_VERTICALS); defaults to the Summit Roofing Co.
demo tenant used across the agentic apps. Sends are approval-gated.

  GET  /health        -> {"status","core":"twenty","connected": <Twenty /healthz>}
  GET  /api/pipeline  -> ranked accounts + picked play + drafted opener + approval state
  GET  /              -> MD3 dashboard (source, rank, draft, approve — the GUI)
  POST /agent/run     -> {"action":"refresh"|"approve"|"send_all", "account": "..."}

Config (env): TWENTY_URL · TWENTY_API_KEY · OPENAI_API_KEY(gpt-5.5) · OUTREACH_TENANT/PITCH/VERTICALS ·
GITHUB_TOKEN · SAMGOV_KEY · ADZUNA_APP_ID/KEY (each source degrades gracefully when its key is absent).
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

PORT = int(os.environ.get("PORT", "8214"))
PLANNER = "https://redevops.io/planner"

# The pure Twenty CRM client, signal scoring + pipeline build, and the /agent/run action verbs
# (refresh / approve / send_all) live in core.py — pure stdlib, no web / context-runtime / LLM — so
# the Mission Runtime operator can invoke them and they can be tested against a fake Twenty. The
# LLM-driven discovery + conversational layer stays here and calls into core.
from . import core
from .core import (  # noqa: E402
    TWENTY_URL, TWENTY_API_KEY, TENANT, PITCH, TWENTY_PUBLIC_URL, _STATE,
    _get, build_pipeline, twenty_connected, twenty_sync, _pilot_amount, twenty_opportunities,
)

# Workspace default lead priorities for discovery (used by plan_search); otherwise discovery is
# driven purely by what the user asks for.
DEFAULT_VERTICALS = [v.strip() for v in os.environ.get("OUTREACH_VERTICALS", "").split(",") if v.strip()]


# ──────────────────────────── app + endpoints ────────────────────────────

app = FastAPI(title="agentic-outreach-engine (core: Twenty CRM)")


@app.get("/health")
def health():
    return {"status": "up", "core": "twenty", "connected": twenty_connected()}


@app.get("/api/pipeline")
def api_pipeline(live: int = 1):
    return JSONResponse({"accounts": build_pipeline(bool(live)), "connected": twenty_connected()})


@app.post("/api/run")        # proxied path (control-plane forwards /m/<app>/api/* only)
@app.post("/agent/run")      # kept for internal/control-plane callers
async def agent_run(req: Request):
    body = await req.json()
    action = body.get("action", "refresh")
    # The action verbs live in core.py (pure, operator-invocable). This endpoint is a thin dispatch.
    if action == "refresh":
        return core.refresh(body)
    if action == "approve":                      # human sign-off → queue + sync to Twenty
        return core.approve(body)
    if action == "send_all":
        return core.send_all(body)
    return {"ok": False, "error": f"unknown action {action}"}


# ──────────────────────── the CRM discovery agent (transparent lead-gen) ────────────────────────
# A user says what leads they want in plain language; the agent PLANS a search, runs discovery,
# adds matches to the CRM, and RETURNS ITS WORK — the plan, what it searched, what it added, what it
# skipped and why, or why it found nothing. No black box: that transparency is the whole point.

_STOP = {"the","a","an","for","with","that","are","and","or","of","to","in","on","who","find","get",
         "me","some","any","leads","lead","companies","company","startups","startup","firms","teams",
         "using","build","building","want","looking","need","i","we","our","their","more","than","at",
         "recently","new","around","about","stars","star"}


def _post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        return json.loads(r.read().decode())


# ── discovery for the REAL ICP: domain orgs making sense of their records + adding agentic access.
# GitHub (RAG vendors) was the wrong source. We target ORGANIZATIONS by their data/records-investment
# signal — gov awards/procurement, hiring, digitization news — across healthcare, insurance, real
# estate, state/municipal finance, education, rentals and services. KEYLESS sources (USASpending,
# GDELT) run now; SAM.gov + a jobs board switch on when SAMGOV_KEY / ADZUNA_APP_ID+KEY are set.

VERTICAL_CUES = {
    "healthcare": ["health", "medical", "patient", "clinical", "hospital", "ehr"],
    "insurance": ["insurance", "insurer", "claims", "underwriting", "policyholder"],
    "real estate": ["real estate", "property", "realty", "title", "brokerage"],
    "state/municipal finance": ["county", "municipal", "city of", "state of", "treasury", "budget", "comptroller"],
    "education": ["school", "district", "university", "college", "campus", "student"],
    "rentals": ["rental", "property management", "leasing", "tenant", "landlord"],
    "services": ["services", "firm", "practice"],
}
_BASE_SIGNAL = ["records management", "document management", "case management", "records digitization",
                "data modernization", "electronic records", "data platform", "content management"]
# Real procurement phrases that actually appear in federal award descriptions (probed against USASpending),
# per vertical — used to search gov awards/grants so the recipients are real, matchable organizations.
_PROCUREMENT_TERMS = {
    "healthcare": ["electronic health records", "health information", "medical records"],
    "insurance": ["claims processing", "claims management"],
    "real estate": ["land records", "property records", "title records"],
    "state/municipal finance": ["financial management system", "records management", "case management"],
    "education": ["student information system", "student records"],
    "rentals": ["property management", "records management"],
    "services": ["document management", "records management", "case management"],
}


def plan_search(instruction: str) -> dict:
    low = instruction.lower()
    verts = [v for v, cues in VERTICAL_CUES.items() if any(c in low for c in cues)] or DEFAULT_VERTICALS
    mc = re.search(r"top\s+(\d+)|add\s+(\d+)|(\d+)\s+(?:leads|opportunities|orgs|companies)", low)
    count = int(next((g for g in mc.groups() if g), 5)) if mc else 5
    kws = list(dict.fromkeys([v + " records" for v in verts] + _BASE_SIGNAL))[:8]
    plan = {"keywords": kws, "verticals": verts, "count": min(count, 10), "how": "keyword map (no model configured)"}
    try:
        j = json.loads(_json_slice(_llm(
            "You plan lead discovery for " + TENANT + ". Turn the instruction into JSON "
            '{"keywords":[3-8 short search phrases to find matching organizations], '
            '"verticals":[relevant industry/segment tags], "count":int}. Instruction: ' + instruction, 300)))
        if j.get("keywords"):
            plan.update(keywords=[str(k) for k in j["keywords"]][:8], verticals=j.get("verticals") or verts,
                        count=min(int(j.get("count", count)), 10), how="planned by gpt-5.5")
    except Exception:
        pass
    return plan


def _src_usaspending(plan: dict) -> list:
    """KEYLESS. Federal awards/grants whose description matches records/data work — the recipient
    (a vendor, agency, or a state/local government) is a real, followable lead with a $ amount."""
    verts = plan.get("verticals") or []
    terms = list(dict.fromkeys([t for v in verts for t in _PROCUREMENT_TERMS.get(v, [])]))[:6] or ["records management", "case management"]
    out, seen = [], set()
    for group in (["A", "B", "C", "D"], ["02", "03", "04", "05"]):   # contracts, then grants (one group/query)
        body = {"filters": {"keywords": terms, "award_type_codes": group,
                            "award_amounts": [{"lower_bound": 50000, "upper_bound": 8000000}],  # skip mega-contractors
                            "time_period": [{"start_date": "2023-01-01", "end_date": "2026-12-31"}]},
                "fields": ["Recipient Name", "Award Amount", "Description", "Awarding Agency", "recipient_id"],
                "page": 1, "limit": 15, "sort": "Award Amount", "order": "desc"}
        try:
            d = _post_json("https://api.usaspending.gov/api/v2/search/spending_by_award/", body)
        except Exception:
            continue
        for r in d.get("results", []):
            name = (r.get("Recipient Name") or "").strip()
            if not name or name.lower() in seen or name.upper() in ("MULTIPLE RECIPIENTS", "REDACTED DUE TO PII", "MISCELLANEOUS FOREIGN AWARDEES"):
                continue
            seen.add(name.lower())
            rid = r.get("recipient_id")
            out.append({"account": name.title() if name.isupper() else name, "signal": "gov_spend", "source": "usaspending",
                        "evidence": ((r.get("Description") or "federal award")[:120]) + " · " + (r.get("Awarding Agency") or ""),
                        "url": ("https://www.usaspending.gov/recipient/" + rid + "/latest") if rid else "https://www.usaspending.gov/",
                        "amount": int(r.get("Award Amount") or 0)})
    return out


def _src_gdelt(keywords: list) -> list:
    """KEYLESS. Recent news about records/data digitization (subject-org extraction is fuzzy — the
    report is transparent that these are news signals, publisher-domain as the handle)."""
    q = '"' + (keywords[0] if keywords else "records digitization") + '"'
    url = "https://api.gdeltproject.org/api/v2/doc/doc?format=json&mode=artlist&maxrecords=15&sort=datedesc&query=" + urllib.parse.quote(q)
    try:
        d = _get(url)
    except Exception:
        return []
    out = []
    for a in d.get("articles", []):
        dom = (a.get("domain") or "").strip()
        if dom:
            out.append({"account": dom, "signal": "news", "source": "gdelt",
                        "evidence": (a.get("title") or "")[:130], "url": a.get("url"), "amount": 0})
    return out


def _src_samgov(keywords: list) -> list:
    key = os.environ.get("SAMGOV_KEY")
    if not key:
        return []
    q = urllib.parse.quote(" ".join(keywords[:3]))
    d = _get("https://api.sam.gov/opportunities/v2/search?limit=15&postedFrom=01/01/2024&postedTo=12/31/2026&q=" + q + "&api_key=" + key)
    out = []
    for o in (d.get("opportunitiesData") or []):
        org = (o.get("organizationName") or o.get("fullParentPathName") or "").split(".")[-1].strip()
        if org:
            out.append({"account": org, "signal": "rfp", "source": "samgov",
                        "evidence": (o.get("title") or "solicitation")[:130], "url": o.get("uiLink") or "https://sam.gov/", "amount": 0})
    return out


def _src_jobs(keywords: list, verticals: list) -> list:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []
    what = urllib.parse.quote((verticals[0] if verticals else "") + " data records")
    url = ("https://api.adzuna.com/v1/api/jobs/us/search/1?app_id=" + app_id + "&app_key=" + app_key +
           "&results_per_page=20&what=" + what)
    d = _get(url)
    out = []
    for j in d.get("results", []):
        co = ((j.get("company") or {}).get("display_name") or "").strip()
        if co:
            out.append({"account": co, "signal": "hiring", "source": "jobs",
                        "evidence": (j.get("title") or "hiring")[:120], "url": j.get("redirect_url") or "", "amount": 0})
    return out


def run_discovery(plan: dict) -> tuple[list[dict], str]:
    kws = plan["keywords"]
    has_key = {"SAM.gov": bool(os.environ.get("SAMGOV_KEY")),
               "jobs board": bool(os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"))}
    used, cands = [], []
    for label, fn in (("USASpending.gov", lambda: _src_usaspending(plan)),
                      ("SAM.gov", lambda: _src_samgov(kws)),
                      ("jobs board", lambda: _src_jobs(kws, plan.get("verticals", []))),
                      ("GDELT news", lambda: _src_gdelt(kws))):
        try:
            r = fn()
        except Exception:
            r = []
        cands += r
        if label in has_key and not has_key[label]:
            used.append(label + " (needs key)")
        else:
            used.append(label + " (" + str(len(r)) + ")")
    seen, dedup = set(), []
    for c in cands:
        k = (c.get("account") or "").lower()
        if k and k not in seen:
            seen.add(k)
            dedup.append(c)
    return dedup, "; ".join(used)


def do_discover(instruction: str) -> dict:
    plan = plan_search(instruction)
    try:
        cands, sources = run_discovery(plan)
    except Exception as e:
        return {"intent": "discover", "instruction": instruction, "plan": plan, "found": 0, "added": [], "skipped": [],
                "note": "Discovery failed (" + type(e).__name__ + "). A source may be rate-limited — try again shortly."}
    if not cands:
        return {"intent": "discover", "instruction": instruction, "plan": plan, "searched": sources, "found": 0, "added": [], "skipped": [],
                "note": "No matching organizations found. Name a vertical (healthcare, insurance, real estate, municipal finance…) or broaden the records/data phrasing."}
    existing = {a["account"].lower() for a in _STATE["accounts"]} | {a.lower() for a in _STATE["approved"]}
    added, skipped = [], []
    for c in cands:
        if len(added) >= plan["count"]:
            break
        if c["account"].lower() in existing:
            skipped.append({"account": c["account"], "reason": "already in your pipeline / CRM"})
            continue
        if twenty_sync(c):
            added.append({"account": c["account"], "evidence": c["evidence"], "signal": c.get("source"),
                          "amount": int(c.get("amount") or _pilot_amount(c)), "url": c.get("url")})
            _STATE["approved"].add(c["account"])
            existing.add(c["account"].lower())
        else:
            skipped.append({"account": c["account"], "reason": "CRM write failed (Twenty API error)"})
    return {"intent": "discover", "instruction": instruction, "plan": plan, "searched": sources,
            "found": len(cands), "added": added, "skipped": skipped,
            "note": None if added else "Found candidates but added none — see the skipped reasons."}



@app.post("/api/discover")
async def api_discover(req: Request):
    body = await req.json()
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        return {"ok": False, "error": "Tell me what kind of leads you're after — e.g. “RAG eval startups with 200+ stars”."}
    return {"ok": True, **do_discover(instruction)}


# ──────────────── the conversational CRM agent: one box, many capabilities, all transparent ────────────────

def _tw_get(path: str) -> dict:
    req = urllib.request.Request(TWENTY_URL + path, headers={"Authorization": "Bearer " + TWENTY_API_KEY})
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
        return json.loads(r.read().decode())


def _json_slice(txt: str) -> str:
    a, b = txt.find("{"), txt.rfind("}")
    return txt[a:b + 1] if a >= 0 and b > a else "{}"


# The agent's LLM plane is Context Runtime's ModelPlugin (cost-tiered, accounted), not a
# raw provider SDK — same stack every other agent runs on. Degrades to "" when no key.
try:
    from context_runtime.adapters.model_openai import OpenAICompatibleModel as _CRModel
    from context_runtime.types import ModelRequest as _CRReq
    _CR_LLM = _CRModel.from_env(default_model=os.environ.get("SOCIAL_LLM_MODEL", "gpt-5.5"))
except Exception:  # noqa: BLE001 — context-runtime absent → offline
    _CR_LLM = None
    _CRReq = None


def _llm(prompt: str, max_tokens: int = 400) -> str:
    if _CR_LLM is None or _CRReq is None:
        return ""
    try:
        res = _CR_LLM.complete(_CRReq(messages=({"role": "user", "content": prompt},), max_tokens=max_tokens))
        return res.text
    except Exception:  # noqa: BLE001
        return ""


def classify(message: str) -> dict:
    low = message.lower()
    intent = "discover"
    if any(w in low for w in ("follow up", "follow-up", "followup", "reach out", "reply", "email to", "write to", "nudge")):
        intent = "followup"
    elif any(w in low for w in ("pipeline", "what's in", "hottest", "summar", "how many", "status", "deals", "opportunit")):
        intent = "pipeline"
    elif any(w in low for w in ("what is", "what are", "how do i", "how to", "explain", "workflow", "stage", "help", "what's a")):
        intent = "help"
    m = re.search(r"(?:for|to|about|with)\s+([A-Za-z0-9 .&_-]{2,40})", message)
    out = {"intent": intent, "target": (m.group(1).strip() if m else "")}
    j = None
    try:
        j = json.loads(_json_slice(_llm(
            "Classify this CRM-assistant message. Intents: discover (find/source NEW leads), "
            "followup (draft a follow-up for a named company/opportunity), pipeline (summarize current "
            "opportunities), help (explain a CRM concept like workflows/stages/follow-ups). "
            'Return ONLY JSON {"intent":"...","target":"<company name if followup else empty>"}. '
            "Message: " + message, 150)))
    except Exception:
        j = None
    if j and j.get("intent") in ("discover", "followup", "pipeline", "help"):
        out["intent"] = j["intent"]
        out["target"] = j.get("target") or out["target"]
    return out


def find_opportunity(target: str) -> dict:
    if not TWENTY_API_KEY or not target:
        return {}
    t = target.lower().strip()
    try:
        ops = _tw_get("/rest/opportunities?limit=80")["data"]["opportunities"]
        for o in ops:
            if t in (o.get("name") or "").lower():
                return o
        for o in ops:
            if o.get("companyId"):
                co = _tw_get("/rest/companies/" + o["companyId"])["data"]["company"]
                if t in (co.get("name") or "").lower():
                    return {**o, "_company": co}
    except Exception:
        pass
    return {}


def draft_followup(opp: dict) -> str:
    name = opp.get("name", "this account")
    co = opp.get("_company") or {}
    site = (co.get("domainName") or {}).get("primaryLinkUrl") or ""
    out = _llm("Write a SHORT, friendly 2nd-touch follow-up email (under 120 words) for the opportunity "
               "“" + name + "”" + ((" (" + site + ")") if site else "") + ". You are " + TENANT + " — " + PITCH +
               ". This is a polite nudge after a first note. Suggest a quick call. Output only the email body.", 260)
    if out:
        return out.strip()
    return ("Hi — just following up on my earlier note. At " + TENANT + ", " + PITCH + ". "
            "Happy to put together a quick, no-obligation quote or answer any questions — worth a short call this week?")


def pipeline_summary() -> dict:
    if not TWENTY_API_KEY:
        return {"total": 0, "value": 0, "by_stage": {}, "top": []}
    try:
        ops = _tw_get("/rest/opportunities?limit=80")["data"]["opportunities"]
    except Exception:
        return {"total": 0, "value": 0, "by_stage": {}, "top": []}
    value = sum((o.get("amount") or {}).get("amountMicros", 0) for o in ops) // 1_000_000
    by_stage = {}
    for o in ops:
        st = o.get("stage", "?")
        by_stage[st] = by_stage.get(st, 0) + 1
    top = sorted(ops, key=lambda o: (o.get("amount") or {}).get("amountMicros", 0), reverse=True)[:5]
    top = [{"name": o.get("name"), "amount": (o.get("amount") or {}).get("amountMicros", 0) // 1_000_000} for o in top]
    return {"total": len(ops), "value": value, "by_stage": by_stage, "top": top}


_CRM_PRIMER = (
    "Twenty CRM basics: an OPPORTUNITY is a potential deal (it has an amount, a close date, a STAGE, a linked "
    "COMPANY and a point-of-contact PERSON). STAGES move a deal along: NEW -> SCREENING -> MEETING -> PROPOSAL "
    "-> CUSTOMER. A FOLLOW-UP is just the next outreach on an opportunity; here you draft and send it from the "
    "agent. WORKFLOWS are automations Twenty runs when something happens (e.g. when an opportunity reaches "
    "PROPOSAL, auto-create a task or fire a webhook) — they remove manual steps.")


def explain_crm(question: str) -> str:
    out = _llm("You are a friendly CRM guide. Using this primer, answer the question in 2-4 short plain sentences, "
               "no jargon.\nPrimer: " + _CRM_PRIMER + "\nQuestion: " + question, 300)
    return out.strip() if out else _CRM_PRIMER


# ──────── standardized in-app agent · Context Runtime AgentConsole (same as the other agentic apps) ────────
# Wraps the CRM capabilities (pipeline / draft / discover) as tools behind the SHARED console, so outreach
# gives the same conversational + grounded-support experience as support/billing/etc. Discovery is the only
# side-effecting tool (it writes leads to Twenty); drafts + summaries are read-only. Falls back to the
# built-in router if context-runtime is unavailable.
try:
    from context_runtime.integrations.agent_console import AgentConsole as _AgentConsole, tool as _crtool
except Exception:  # noqa: BLE001
    _AgentConsole = None


def _t_pipeline(_a: dict) -> dict:
    s = pipeline_summary()
    stages = ", ".join(f"{k}: {v}" for k, v in s["by_stage"].items()) or "—"
    top = "; ".join(f"{t['name']} (${t['amount']:,})" for t in s["top"]) or "none yet"
    return {"text": f"{s['total']} opportunities worth ${s['value']:,} total. By stage: {stages}. Top: {top}.",
            "data": s}


def _t_draft_followup(a: dict) -> dict:
    target = (a.get("target") or a.get("account") or a.get("opportunity") or "").strip()
    if not target:
        names = [t["name"] for t in pipeline_summary()["top"]]
        return {"text": "Which opportunity? Current: " + (", ".join(names) or "none yet — run discovery first") + ".", "data": None}
    opp = find_opportunity(target)
    if not opp:
        names = [t["name"] for t in pipeline_summary()["top"]]
        return {"text": f"No opportunity matching “{target}”. Current: " + (", ".join(names) or "none") + ".", "data": None}
    draft = draft_followup(opp)
    return {"text": f"Drafted a follow-up for {opp.get('name')} — review, then send from Twenty:\n\n{draft}",
            "data": {"opportunity": opp.get("name"), "draft": draft}}


def _t_discover(a: dict) -> dict:
    instruction = (a.get("instruction") or a.get("query") or a.get("what") or "").strip()
    if not instruction:
        return {"text": "What kind of leads? Name a vertical (healthcare, insurance, municipal finance…) or the records/data phrasing.", "data": None}
    rep = do_discover(instruction)
    n = len(rep.get("added", []))
    if n:
        accts = ", ".join(x["account"] for x in rep["added"])
        return {"text": f"Added {n} lead{'' if n == 1 else 's'} to Twenty CRM: {accts}.", "data": rep}
    return {"text": rep.get("note") or "Found candidates but added none — see the skipped reasons.", "data": rep}


if _AgentConsole is not None:
    _OUTREACH_CONSOLE = _AgentConsole(
        f"{TENANT} Outreach", _CRM_PRIMER,
        tools=[
            _crtool("pipeline_summary", "summarize the CRM pipeline — total opportunities, value, by-stage, and the top accounts", _t_pipeline),
            _crtool("draft_followup", "draft a 2nd-touch follow-up email for a named opportunity/account (read-only — a human sends it)", _t_draft_followup,
                    parameters={"type": "object", "properties": {"target": {"type": "string", "description": "opportunity or company name"}}}),
            _crtool("discover_leads", "find NEW leads from public signals (awards/news/RFPs/jobs) and add them to Twenty CRM", _t_discover,
                    parameters={"type": "object", "properties": {"instruction": {"type": "string", "description": "what kind of leads to find"}}},
                    side_effecting=True),
        ],
        suggestions=["Summarize my pipeline", "Find leads in municipal finance", "Draft a follow-up for the top account", "What is an opportunity?"],
        subtitle="Ask how the CRM works, find leads, draft follow-ups, or check the pipeline — a human sends anything outbound.",
        allow_side_effects=["discover_leads"],
    )
else:
    _OUTREACH_CONSOLE = None


@app.post("/api/agent")
async def api_agent(req: Request):
    """Conversational endpoint. Uses the standard Context Runtime AgentConsole (grounded support + CRM
    tools) when available; falls back to the built-in intent router if context-runtime is absent."""
    body = await req.json()
    message = (body.get("message") or "").strip()
    if _OUTREACH_CONSOLE is not None:
        return JSONResponse(_OUTREACH_CONSOLE.respond(message))
    if not message:
        return {"ok": False, "say": "Ask me to find leads, draft a follow-up, summarize your pipeline, or explain how the CRM works."}
    cls = classify(message)
    intent = cls["intent"]
    if intent == "discover":
        rep = do_discover(message)
        n = len(rep.get("added", []))
        say = (("Added " + str(n) + " lead" + ("" if n == 1 else "s") + " to your CRM.") if n
               else (rep.get("note") or "No leads added."))
        return {"ok": True, "intent": "discover", "say": say, "report": rep}
    if intent == "followup":
        opp = find_opportunity(cls["target"] or message)
        if not opp:
            names = [t["name"] for t in pipeline_summary()["top"]]
            return {"ok": True, "intent": "followup", "found": False,
                    "say": "I couldn't find an opportunity matching “" + (cls["target"] or "that") + "”. "
                           "Your current opportunities: " + (", ".join(names) if names else "none yet — try discovery first") + "."}
        return {"ok": True, "intent": "followup", "found": True, "opportunity": opp.get("name"),
                "draft": draft_followup(opp),
                "say": "Drafted a follow-up for " + str(opp.get("name")) + ". Review it, then send from Twenty (or ask me to log it)."}
    if intent == "pipeline":
        s = pipeline_summary()
        stages = ", ".join(k + ": " + str(v) for k, v in s["by_stage"].items()) or "—"
        return {"ok": True, "intent": "pipeline",
                "say": str(s["total"]) + " opportunities worth $" + format(s["value"], ",") + " total. By stage: " + stages + ".",
                "summary": s}
    return {"ok": True, "intent": "help", "say": explain_crm(message)}



@app.get("/api/synced")
def api_synced():
    """Opportunities read back from Twenty — proves the sync end-to-end over https."""
    opps = twenty_opportunities()
    return JSONResponse({"opportunities": opps, "authed": bool(TWENTY_API_KEY),
                         "crm_url": TWENTY_PUBLIC_URL})


@app.get("/", response_class=HTMLResponse)
def dashboard():
    rows = _STATE["accounts"] or build_pipeline()
    conn = twenty_connected()
    page = render(rows, conn)
    # inject the standard Context Runtime agent console (same panel the other agentic apps use)
    panel = (f'<div class="card">{_OUTREACH_CONSOLE.panel_html("outreach")}</div>'
             if _OUTREACH_CONSOLE is not None else "")
    return HTMLResponse(page.replace("<!--AGENT_PANEL-->", panel))


# ──────────────────────────── MD3 dashboard ────────────────────────────

BASE_CSS = """
:root{--surface:#131316;--surface-container:#1f1f23;--surface-container-high:#2a2a2e;--outline-variant:#2f2f33;
--on-surface:#e4e2e6;--on-surface-variant:#c7c5ca;--on-surface-muted:#918f96;--primary:#4fd1c5;
--on-primary:#00201c;--primary-container:#00504a;--on-primary-container:#a8f0e6;--secondary:#f5b544;
--success:#5bd98a;--warning:#f5b544;--danger:#f2544f;--radius:16px;--radius-pill:999px;
--font:"Roboto",system-ui,-apple-system,"Segoe UI",sans-serif;--mono:ui-monospace,"SF Mono",monospace}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--on-surface);font-family:var(--font);padding:24px}
.shell{max-width:1200px;margin:0 auto;display:flex;flex-direction:column;gap:20px}
.top{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
h1{font:500 24px/32px var(--font);margin:0}.sub{color:var(--on-surface-muted);font-size:13px;margin-top:2px}
.pill{font:500 12px/1 var(--font);padding:6px 12px;border-radius:var(--radius-pill);display:inline-flex;gap:6px;align-items:center}
.pill.ok{background:#0f3d22;color:var(--success)}.pill.off{background:#4a3500;color:var(--warning)}
.kpis{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.kpi{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius);padding:18px}
.kpi .n{font:600 30px/1 var(--font);color:var(--primary)}.kpi .l{color:var(--on-surface-muted);font-size:12px;margin-top:6px;text-transform:uppercase;letter-spacing:.5px}
.card{background:var(--surface-container);border:1px solid var(--outline-variant);border-radius:var(--radius);padding:20px}
.acct{border-top:1px solid var(--outline-variant);padding:16px 0}.acct:first-of-type{border-top:0}
.arow{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.name{font:500 16px/1 var(--font)}.chip{font:500 11px/1 var(--mono);padding:4px 8px;border-radius:6px;background:var(--surface-container-high);color:var(--on-surface-variant)}
.chip.sig{background:var(--primary-container);color:var(--on-primary-container)}.chip.play{background:#173a36;color:var(--primary)}
.score{margin-left:auto;font:600 14px/1 var(--mono);color:var(--primary)}
.ev{color:var(--on-surface-muted);font-size:12.5px;margin:8px 0}
.draft{background:#17171a;border:1px solid var(--outline-variant);border-radius:10px;padding:12px;font-size:13px;white-space:pre-wrap;color:var(--on-surface-variant)}
.draft b{color:var(--on-surface)}
.btn{font:500 13px/1 var(--font);padding:9px 16px;border-radius:var(--radius-pill);border:0;cursor:pointer;background:var(--primary);color:var(--on-primary)}
.btn.gate{background:var(--surface-container-high);color:var(--warning);border:1px solid var(--warning)}
.btn.done{background:#0f3d22;color:var(--success);border:1px solid var(--success)}
.actions{display:flex;gap:10px;margin-top:12px;align-items:center}
.foot{color:var(--on-surface-muted);font-size:12px}
.chip.off{background:#4a3500;color:var(--warning);font-family:var(--font)}
.synced-list{display:flex;flex-direction:column;gap:2px}
.srow{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px 0;border-top:1px solid var(--outline-variant)}
.srow:first-child{border-top:0}.srow .foot{margin-left:auto}
a{color:var(--primary)}
"""


def _esc(v) -> str:
    return html.escape(str(v))


def render(rows: list[dict], conn: bool) -> str:
    pending = sum(1 for r in rows if not r["approved"])
    high = sum(1 for r in rows if r["signal"] in ("tech_pain", "funding"))
    kpis = [("Accounts sourced", len(rows)), ("High-signal", high),
            ("Drafts ready", len(rows)), ("Awaiting approval", pending)]
    kpi_html = "".join(f'<div class="kpi"><div class="n">{n}</div><div class="l">{_esc(l)}</div></div>' for l, n in kpis)
    conn_pill = ('<span class="pill ok">● Twenty CRM connected</span>' if conn
                 else '<span class="pill off">● Twenty CRM coming online</span>')
    accts = ""
    for r in rows:
        d = r["draft"]
        btn = ('<button class="btn done" disabled>✓ approved · synced</button>' if r["approved"]
               else f'<button class="btn gate" onclick="approve(this,\'{_esc(r["account"])}\')">⏸ approve &amp; sync to Twenty</button>')
        art = f' · <a href="{_esc(r["artifact"])}" target="_blank">artifact</a>' if r.get("artifact") else ""
        accts += (
            f'<div class="acct"><div class="arow"><span class="name">{_esc(r["account"])}</span>'
            f'<span class="chip sig">{_esc(r["signal"])}</span><span class="chip play">play: {_esc(r["play"])}</span>'
            f'<span class="chip">{_esc(r["source"])}</span><span class="score">{r["score"]}</span></div>'
            f'<div class="ev">{_esc(r["evidence"])}{art}</div>'
            f'<div class="draft"><b>{_esc(d["subject"])}</b>\n\n{_esc(d["body"])}</div>'
            f'<div class="actions">{btn}<span class="foot">nothing sends without your sign-off</span></div></div>')
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Outreach Engine · {TENANT}</title>
<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;600&family=Roboto+Mono&display=swap">
<style>{BASE_CSS}</style></head><body><div class=shell>
<div class=top><div><h1>Outreach Engine</h1><div class=sub>Find leads, draft follow-ups, and track opportunities — a conversational agent over your <b>Twenty CRM</b>. Tenant: <b>{_esc(TENANT)}</b>.</div></div>
<div style="display:flex;gap:10px;align-items:center">{conn_pill}
<button class="btn" onclick="refresh(this,0)">↻ Refresh</button>
<button class="btn" onclick="refresh(this,1)">⚡ Pull live signals</button></div></div>
<div class=kpis>{kpi_html}</div>
<!--AGENT_PANEL-->
<div class=card><div style="font:500 16px/1 var(--font);margin-bottom:4px">Ranked accounts &amp; drafted outreach</div>
<div class=foot style="margin-bottom:8px">Signal → ICP score → the play the runtime picked → the opener → your approval. Approving syncs a pilot opportunity to Twenty CRM.</div>
{accts}</div>
<div class=card id=synced>
<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap">
<div style="font:500 16px/1 var(--font)">Synced to Twenty CRM</div>
<a id=crmlink class=btn href="{_esc(TWENTY_PUBLIC_URL)}" target=_blank rel=noopener>Open Twenty CRM ↗</a></div>
<div class=foot style="margin:4px 0 8px">Read straight back from Twenty — the opportunities this agent created on approval. Proves the sync end-to-end.</div>
<div id=synced-body class=foot>loading…</div></div>
<div class=foot>Discovery sources: government awards · news · RFPs · job postings (each configurable per workspace). Every action shows its work — no black box.</div>
</div>
<script>
async function post(b){{const r=await fetch('api/run',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b)}});return r.json()}}
async function approve(el,a){{el.disabled=true;el.textContent='…';await post({{action:'approve',account:a}});location.reload()}}
async function refresh(el,live){{el.disabled=true;el.textContent='…';await post({{action:'refresh',live:!!live}});location.reload()}}
function esc(s){{return String(s).replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]))}}
async function loadSynced(){{
  const el=document.getElementById('synced-body');
  try{{
    const r=await fetch('api/synced');const d=await r.json();
    document.getElementById('crmlink').href=d.crm_url||'#';
    if(!d.authed){{el.innerHTML='<span class="chip off">Twenty API key not set — approve an account once the key is wired to see synced pilots here.</span>';return}}
    const os=d.opportunities||[];
    if(!os.length){{el.textContent='No opportunities yet — approve a ranked account above and it appears here.';return}}
    el.innerHTML='<div class="synced-list">'+os.map(o=>{{
      const via=o.via_api?'<span class="chip play">via agent</span>':'<span class="chip">manual</span>';
      const nm=o.url&&o.url.indexOf('/undefined')<0?`<a href="${{esc(o.url)}}" target=_blank rel=noopener>${{esc(o.name)}}</a>`:esc(o.name);
      return `<div class="srow"><span class="name">${{nm}}</span><span class="chip sig">${{esc(o.stage)}}</span>${{via}}<span class="foot">${{esc(o.created_at)}}</span></div>`;
    }}).join('')+'</div>';
  }}catch(e){{el.textContent='Could not reach Twenty.';}}
}}
// discovery writes to Twenty; refresh the "Synced to Twenty CRM" list shortly after the agent runs.
document.addEventListener('ac:responded', function(){{setTimeout(loadSynced,1500);}});
loadSynced();
</script></body></html>"""


# ── Mission Runtime operator surface (Phase-1 production wiring) ──
# GET /capabilities + POST /invoke, so the runtime can drive outreach as an operator.
# Guarded: if agentic-os isn't installed, the app still runs standalone.
try:
    from .operator import build_outreach_operator
    app.include_router(build_outreach_operator().router())
except Exception:  # noqa: BLE001 — agentic_os absent → no operator surface
    pass
