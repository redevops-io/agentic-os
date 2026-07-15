"""lifecycle core — the pure Listmonk REST client + agentic actions.

No web framework, no context-runtime: just stdlib + httpx against a real Listmonk core.
This is the layer the FastAPI app renders from AND the Mission Runtime operator invokes, so
the capability handlers can be tested against a fake Listmonk without booting the whole app.

The three actions take an optional `llm` callback (`(prompt, max_tokens) -> str | None`) that
generates the campaign / segment / flow copy. The real model plane lives in `app.py`
(context-runtime); with `llm=None` the actions stay fully deterministic — compose still POSTs
a Listmonk draft, segment/suggest_flow return their skeleton — keeping this module
dependency-light and testable against a fake core.
"""
from __future__ import annotations

import html
import json
import os
import time
from pathlib import Path
from typing import Callable

import httpx

# ── config (env; seed.py writes agents/lifecycle/.env) ───────────────────────
# Idempotent .env load so this module is self-sufficient when imported by the operator
# (i.e. without the FastAPI app having run its own loader first).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

LISTMONK_API_URL = os.environ.get("LISTMONK_API_URL", "http://localhost:9000").rstrip("/")
LISTMONK_API_USER = os.environ.get("LISTMONK_API_USER", "agentic")
LISTMONK_API_TOKEN = os.environ.get("LISTMONK_API_TOKEN", "")
LISTMONK_FRONT_URL = os.environ.get("LISTMONK_FRONT_URL", "http://localhost:9000").rstrip("/")
TENANT = os.environ.get("LIFECYCLE_TENANT", "Summit Roofing Co.")
SUBTITLE = ("Lifecycle email/SMS that composes campaigns, proposes segments, and drafts flows "
            "on a self-hosted Listmonk core — a human reviews + sends; nothing auto-blasts a list.")

# An LLM callback: (prompt, max_tokens) -> generated text or None. Injected from app.py.
Llm = Callable[[str, int], "str | None"]


# ── Listmonk REST client ─────────────────────────────────────────────────────
def _headers() -> dict:
    # Listmonk v3+ API-user auth is `Authorization: token <user>:<token>` (NOT Basic).
    return {"Authorization": f"token {LISTMONK_API_USER}:{LISTMONK_API_TOKEN}",
            "Content-Type": "application/json"}


def listmonk_connected() -> bool:
    """Up + the API token authenticates. `/health` is the public liveness endpoint
    (v6); we then confirm the token works against an authed endpoint so "connected"
    means *usable*, not just up."""
    try:
        if httpx.get(f"{LISTMONK_API_URL}/health", timeout=4.0).status_code != 200:
            return False
        if not LISTMONK_API_TOKEN:
            return True
        r = httpx.get(f"{LISTMONK_API_URL}/api/lists", headers=_headers(),
                      params={"per_page": 1}, timeout=5.0)
        return r.status_code == 200
    except Exception:
        return False


def _get(path: str, params: dict | None = None) -> dict:
    try:
        with httpx.Client(timeout=12.0) as c:
            r = c.get(LISTMONK_API_URL + path, headers=_headers(), params=params or {})
            r.raise_for_status()
            return r.json().get("data", {})
    except Exception:
        return {}


# ── live data + KPIs (cached briefly) ────────────────────────────────────────
_CACHE: dict = {"ts": 0.0, "data": None}


def fetch_activity(force: bool = False) -> dict:
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < 15.0:
        return _CACHE["data"]
    connected = listmonk_connected()
    lists = _get("/api/lists", {"per_page": 100}).get("results", []) if connected else []
    camps = _get("/api/campaigns", {"per_page": 50}).get("results", []) if connected else []
    subs_meta = _get("/api/subscribers", {"per_page": 1}) if connected else {}
    total_subs = subs_meta.get("total", sum(l.get("subscriber_count", 0) for l in lists))

    sent = [c for c in camps if c.get("status") == "finished"]
    running = [c for c in camps if c.get("status") in ("running", "scheduled")]
    drafts = [c for c in camps if c.get("status") == "draft"]

    recent = [{"name": c.get("name"), "subject": c.get("subject", ""),
               "status": c.get("status"), "lists": ", ".join(l.get("name", "") for l in c.get("lists", [])),
               "sent": c.get("sent", 0), "views": (c.get("views") or 0)}
              for c in sorted(camps, key=lambda c: c.get("created_at") or "", reverse=True)[:20]]

    list_rows = [{"name": l.get("name"), "type": l.get("type"),
                  "subs": l.get("subscriber_count", 0)} for l in lists]

    data = {
        "tenant": TENANT, "core": "listmonk", "connected": connected,
        "front_url": LISTMONK_FRONT_URL,
        "kpis": [
            {"label": "Subscribers", "value": f"{total_subs:,}", "note": f"{len(lists)} lists"},
            {"label": "Campaigns sent", "value": str(len(sent)), "note": f"{len(running)} running · {len(drafts)} drafts"},
            {"label": "Total opens", "value": f"{sum((c.get('views') or 0) for c in camps):,}", "note": "across all campaigns"},
            {"label": "Drafts ready", "value": str(len(drafts)), "note": "agent-composed, awaiting review"},
        ],
        "lists": list_rows, "recent": recent,
    }
    _CACHE.update(ts=now, data=data)
    return data


# ── agentic actions (deterministic Listmonk API work; LLM copy is injected) ──
def compose_campaign(body: dict, llm: Llm | None = None) -> dict:
    """Write a campaign DRAFT into the real Listmonk core.

    `llm` (optional) generates the subject + HTML body from the brief; without it the
    payload falls back to the brief text. Either way this POSTs a **draft** campaign
    (status "draft") — it never sends or enqueues to contacts, so a human reviews + sends
    in Listmonk. The POST itself is a deterministic Listmonk API call.
    """
    prompt = (body.get("prompt") or "").strip()
    list_id = body.get("list_id")
    if not prompt:
        return {"status": "error", "error": "prompt required", "action": "compose_campaign"}
    out = llm(
        f"You are a lifecycle email marketer for {TENANT}. From this brief, write ONE campaign. "
        "Return STRICT JSON: {\"name\":\"<internal name>\",\"subject\":\"<=60 chars\","
        "\"body_html\":\"<clean responsive HTML, inline styles, one CTA button>\"}. "
        "No fake discounts/specifics.\n\nBRIEF: " + prompt, 900) if llm else None
    camp = {}
    if out:
        try:
            camp = json.loads(out[out.find("{"): out.rfind("}") + 1])
        except Exception:
            camp = {"name": prompt[:60], "subject": prompt[:60], "body_html": f"<p>{html.escape(out)[:2000]}</p>"}
    payload = {"name": camp.get("name", prompt[:60]), "subject": camp.get("subject", prompt[:60]),
               "lists": [int(list_id)] if list_id else [], "type": "regular",
               "content_type": "html", "body": camp.get("body_html", ""), "messenger": "email"}
    created = {}
    try:
        with httpx.Client(timeout=20.0) as c:
            r = c.post(LISTMONK_API_URL + "/api/campaigns", headers=_headers(), json=payload)
            created = r.json().get("data", {}) if r.status_code in (200, 201) else {"_err": r.text[:200]}
    except Exception as e:
        created = {"_err": str(e)}
    return {"status": "done" if created.get("id") else "partial", "action": "compose_campaign",
            "campaign_id": created.get("id"), "name": payload["name"], "subject": payload["subject"],
            "listmonk_status": "draft", "requires": "human review + send in Listmonk",
            "error": created.get("_err")}


def segment(body: dict, llm: Llm | None = None) -> dict:
    """Advisory: propose a Listmonk advanced SQL subscriber query for a targeting goal.

    Read-only — no Listmonk write. `llm` drafts the query; without it the skeleton is
    returned so a human can paste + materialise the segment in Listmonk.
    """
    goal = (body.get("goal") or "").strip()
    if not goal:
        return {"status": "error", "error": "goal required", "action": "segment"}
    out = llm(
        "You are a Listmonk segmentation expert. Given a targeting goal, return a Listmonk "
        "advanced SQL subscriber query (the WHERE expression over `subscribers`, `subscribers.attribs` "
        "JSONB, and subscriber_lists). Return STRICT JSON: {\"name\":\"...\",\"query\":\"<SQL WHERE>\","
        "\"rationale\":\"...\"}.\n\nGOAL: " + goal, 400) if llm else None
    seg = {}
    if out:
        try:
            seg = json.loads(out[out.find("{"): out.rfind("}") + 1])
        except Exception:
            seg = {"name": goal[:50], "query": out[:300], "rationale": ""}
    return {"status": "done", "action": "segment", "name": seg.get("name"),
            "query": seg.get("query"), "rationale": seg.get("rationale"),
            "note": "Paste the query into a Listmonk advanced segment to materialise it."}


def suggest_flow(body: dict, llm: Llm | None = None) -> dict:
    """Advisory: outline a multi-step lifecycle flow and sketch each message.

    Read-only — no Listmonk write. `llm` designs the steps; without it the skeleton is
    returned for a human to implement as templates + a scheduler.
    """
    trigger = (body.get("trigger") or "welcome").strip()
    out = llm(
        f"You are a lifecycle-marketing strategist for {TENANT}. Design a '{trigger}' email flow: "
        "3-5 steps with timing, goal per step, subject line, and a 2-sentence body sketch. "
        "Return STRICT JSON: {\"flow\":\"...\",\"steps\":[{\"delay\":\"...\",\"goal\":\"...\","
        "\"subject\":\"...\",\"body\":\"...\"}]}.", 800) if llm else None
    flow = {}
    if out:
        try:
            flow = json.loads(out[out.find("{"): out.rfind("}") + 1])
        except Exception:
            flow = {"flow": trigger, "steps": [], "raw": out[:1500]}
    return {"status": "done", "action": "suggest_flow", "trigger": trigger, **flow,
            "note": "Implement steps as Listmonk transactional templates + a scheduler/automation."}
