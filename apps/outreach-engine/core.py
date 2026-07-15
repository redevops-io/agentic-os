"""agentic-outreach-engine core — the pure Twenty CRM client + agentic actions.

No web framework, no context-runtime, no LLM: just stdlib urllib against a real Twenty CRM
core (plus the public GitHub/HN signal sources). This is the layer the FastAPI app renders
from AND the Mission Runtime operator invokes, so the capability handlers can be tested
against a fake Twenty without booting the whole console.

The LLM-driven pieces (lead-discovery planning, follow-up drafting, the conversational
router) stay in `app.py` — this module is dependency-light and deterministic so the three
`/agent/run` actions (refresh / approve / send_all) run identically in-process, over HTTP,
and under test.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request

# ── config (env) ─────────────────────────────────────────────────────────────
TWENTY_URL = os.environ.get("TWENTY_URL", "http://twenty:3000").rstrip("/")
TWENTY_API_KEY = os.environ.get("TWENTY_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Public Twenty origin (browsable UI) — read from env so the read-back panel can deep-link a
# synced opportunity into the real CRM over https. Falls back to the internal URL.
TWENTY_PUBLIC_URL = os.environ.get("TWENTY_PUBLIC_URL", "https://crm.redevops.io").rstrip("/")

# ── tenant / product framing — configurable so the CRM agent works for ANY workspace. Defaults to
# Summit Roofing Co. (the demo company used across the agentic apps).
TENANT = os.environ.get("OUTREACH_TENANT", "Summit Roofing Co.")
PITCH = os.environ.get("OUTREACH_PITCH",
    "we handle roof replacements, storm-damage repair and inspections — with fast quotes and a crew that shows up on time")

# ── the learned policy (what the Context Runtime bandit converges to per account bucket) ──
PLAY = {"tech_pain": "video · artifact", "funding": "multi · artifact", "hiring": "multi · artifact",
        "leadership": "linkedin · company", "cold": "email · template"}
SIGNAL_PRIORITY = {"tech_pain": 1.0, "funding": 0.9, "hiring": 0.8, "leadership": 0.7, "cold": 0.15}
SOURCE_WEIGHT = {"github": 1.0, "hn_hiring": 0.9, "manual": 0.85, "hn": 0.6}
HOOK = {"tech_pain": "saw {a} is deep in production RAG",
        "funding": "congrats on the raise — the post-round build window is the moment for this",
        "hiring": "saw {a} is hiring for ML/RAG", "leadership": "saw you just took over AI at {a}",
        "cold": "noticed {a} is building on LLMs"}

# ── canned prospect signals (offline default; refresh pulls live GitHub+HN) ──
DEMO = [
    {"account": "Front Range Property Management", "signal": "cold", "source": "manual",
     "evidence": "manages 40+ commercial properties across the metro — recurring roof maintenance + storm response"},
    {"account": "Cornerstone Commercial Realty", "signal": "cold", "source": "manual",
     "evidence": "owns 8 retail centers; recent hail event in the service area"},
    {"account": "Summit Ridge HOA", "signal": "cold", "source": "manual",
     "evidence": "120-home community with several roofs aging past 20 years"},
]

_STATE = {"accounts": [], "approved": set()}   # in-memory pipeline + approval set


# ──────────────────────────── signals + scoring + teardown ────────────────────────────

def _get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310
        return json.loads(r.read().decode())


def live_signals(limit: int = 8) -> list[dict]:
    out = []
    try:
        h = {"Accept": "application/vnd.github+json", "User-Agent": "outreach"}
        if GITHUB_TOKEN:
            h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        q = urllib.parse.quote("rag retrieval reranking OR llamaindex OR langchain in:readme stars:>50")
        for repo in _get(f"https://api.github.com/search/repositories?sort=updated&per_page={limit}&q={q}", h).get("items", []):
            o = repo.get("owner") or {}
            out.append({"account": o.get("login"), "signal": "tech_pain", "source": "github",
                        "evidence": f"builds RAG in {repo.get('full_name')} ({repo.get('stargazers_count',0)}★)",
                        "url": repo.get("html_url"), "artifact": repo.get("html_url"),
                        "full_name": repo.get("full_name"),
                        "stars": repo.get("stargazers_count", 0)})
    except Exception:
        pass
    try:
        q = urllib.parse.quote("RAG retrieval reranking hallucination")
        for hit in _get(f"https://hn.algolia.com/api/v1/search_by_date?tags=comment&hitsPerPage=6&query={q}",
                        {"User-Agent": "outreach"}).get("hits", []):
            t = re.sub(r"<[^>]+>", " ", hit.get("comment_text") or "")[:140].strip()
            if t:
                out.append({"account": hit.get("author"), "signal": "tech_pain", "source": "hn",
                            "evidence": f"RAG pain: “{t}”",
                            "url": f"https://news.ycombinator.com/item?id={hit.get('objectID')}"})
    except Exception:
        pass
    return [s for s in out if s.get("account")]


def score(s: dict) -> float:
    base = SIGNAL_PRIORITY.get(s["signal"], 0.15) * SOURCE_WEIGHT.get(s["source"], 0.5)
    if s.get("artifact"):
        base += 0.25
    base += min(0.20, s.get("stars", 0) / 5000.0)
    return round(min(1.0, base), 3)


def teardown(s: dict) -> dict:
    a = s["account"]
    body = ("Hi — I came across " + a + " and wanted to reach out. At " + TENANT + ", " + PITCH + ".\n\n"
            "If " + a + " is weighing options, I'd be glad to put together a quick, no-obligation quote and "
            "answer any questions.\n\nWorth a short call this week?")
    return {"subject": a + ": a quick note from " + TENANT, "body": body}


_LIVE_CACHE = {"ts": 0.0, "rows": []}


def _cached_live() -> list[dict]:
    """Live GitHub/HN signals, cached ~10 min so page loads don't hammer the APIs."""
    if time.time() - _LIVE_CACHE["ts"] < 600 and _LIVE_CACHE["rows"]:
        return _LIVE_CACHE["rows"]
    rows = live_signals()
    if rows:
        _LIVE_CACHE.update(ts=time.time(), rows=rows)
    return rows or _LIVE_CACHE["rows"]


def build_pipeline(live: bool = True) -> list[dict]:
    # Real leads by default (live GitHub/HN); the canned DEMO set is only the offline fallback.
    raw = (_cached_live() or DEMO) if live else list(DEMO)
    seen, rows = set(), []
    for s in sorted(raw, key=score, reverse=True):
        k = (s["account"].lower(), s["signal"])
        if k in seen:
            continue
        seen.add(k)
        rows.append({**s, "score": score(s), "play": PLAY.get(s["signal"], PLAY["cold"]),
                     "draft": teardown(s), "approved": s["account"] in _STATE["approved"]})
    _STATE["accounts"] = rows
    return rows


# ──────────────────────────── Twenty CRM (the OSS core) ────────────────────────────

def twenty_connected() -> bool:
    for path in ("/healthz", "/health", "/"):
        try:
            urllib.request.urlopen(urllib.request.Request(TWENTY_URL + path), timeout=2.5)  # noqa: S310
            return True
        except Exception:
            continue
    return False


def _github(path: str):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "outreach"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return _get(f"https://api.github.com{path}", h)


def _gh_profile(login: str) -> dict:
    try:
        return _github(f"/users/{login}")
    except Exception:
        return {}


def _gh_top_contributor(full_name: str) -> dict:
    """The most active HUMAN maintainer of the repo — a real, followable point of contact."""
    try:
        for c in _github(f"/repos/{full_name}/contributors?per_page=5"):
            login = c.get("login") or ""
            if c.get("type") == "User" and "[bot]" not in login:
                return _gh_profile(login)
    except Exception:
        pass
    return {}


def _tw_post(path: str, payload: dict) -> dict:
    """POST to Twenty REST; return the created record (Twenty wraps it as data.create<Object>)."""
    req = urllib.request.Request(TWENTY_URL + path, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {TWENTY_API_KEY}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310
        return next(iter(json.load(r).get("data", {}).values()), {})


def _pilot_amount(row: dict) -> int:
    """A realistic pilot deal size, scaled by traction (repo stars)."""
    return min(55000, 15000 + (int(row.get("stars") or 0) // 50) * 500)


def twenty_sync(row: dict) -> bool:
    """Create a REAL, followable pilot in Twenty for an approved lead: a Company (the org + its
    site/repo), a Person (the repo's top maintainer, with their GitHub profile as the contact),
    and a populated Opportunity (amount · 30-day close · stage · linked to both). Needs TWENTY_API_KEY."""
    if not TWENTY_API_KEY or not isinstance(row, dict):
        return False
    acct = row.get("account", "")
    try:
        full = row.get("full_name") or ""
        owner = _gh_profile(acct) if row.get("source") == "github" else {}
        poc = _gh_top_contributor(full) if full else {}
        domain = (owner.get("blog") or "").strip() or row.get("url") or (f"https://github.com/{acct}" if acct else "")
        company = _tw_post("/rest/companies", {
            "name": owner.get("name") or acct,
            "domainName": {"primaryLinkUrl": domain, "primaryLinkLabel": re.sub(r"^https?://", "", domain)},
        })
        cid = company.get("id")
        pid = None
        if poc:
            parts = (poc.get("name") or poc.get("login") or acct).split(" ", 1)
            person_payload = {
                "name": {"firstName": parts[0], "lastName": parts[1] if len(parts) > 1 else "(maintainer)"},
                "linkedinLink": {"primaryLinkUrl": poc.get("html_url") or "", "primaryLinkLabel": "GitHub"},
                "jobTitle": f"Maintainer · {full or acct}",
                "companyId": cid,
            }
            if poc.get("email"):
                person_payload["emails"] = {"primaryEmail": poc["email"]}
            pid = _tw_post("/rest/people", person_payload).get("id")
        from datetime import datetime, timedelta, timezone
        close = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        _tw_post("/rest/opportunities", {
            "name": f"Pilot — {owner.get('name') or acct}",
            "amount": {"amountMicros": int(row.get("amount") or _pilot_amount(row)) * 1_000_000, "currencyCode": "USD"},
            "closeDate": close, "stage": "NEW", "companyId": cid, "pointOfContactId": pid,
        })
        return True
    except Exception:
        return False


def twenty_opportunities(limit: int = 20) -> list[dict]:
    """Read back the opportunities the agent created in Twenty (the API-sourced pilots).

    Returns [{name, stage, created_at, url}] newest-first, or [] if unauthenticated/unreachable.
    Lets the dashboard prove the sync end-to-end without exposing raw Twenty.
    """
    if not TWENTY_API_KEY:
        return []
    try:
        req = urllib.request.Request(
            TWENTY_URL + f"/rest/opportunities?limit={int(limit)}&order_by=createdAt[DescNullsLast]",
            headers={"Authorization": f"Bearer {TWENTY_API_KEY}"})
        body = json.loads(urllib.request.urlopen(req, timeout=5).read())  # noqa: S310
    except Exception:
        return []
    data = body.get("data", {})
    opps = data.get("opportunities", data) if isinstance(data, dict) else data
    if not isinstance(opps, list):
        return []
    out = []
    for o in opps:
        created_by = (o.get("createdBy") or {}).get("name") or ""
        out.append({
            "name": o.get("name", ""),
            "stage": o.get("stage", ""),
            "created_at": (o.get("createdAt") or "")[:19].replace("T", " "),
            "via_api": (o.get("createdBy") or {}).get("source") == "API",
            "created_by": created_by,
            "url": f"{TWENTY_PUBLIC_URL}/object/opportunity/{o.get('id','')}",
        })
    return out


# ──────────────────────────── agentic actions (the /agent/run verbs) ────────────────────────────
# These are the deterministic, dependency-light actions the console exposes at /agent/run AND the
# Mission Runtime operator invokes. refresh = rebuild the ranked pipeline (reads GitHub/HN, no CRM
# write); approve = human sign-off that SYNCS a pilot to Twenty (CRM write); send_all = dispatch the
# approved sequences to prospects (the gated send — nothing goes out without a human sign-off).

def refresh(body: dict | None = None) -> dict:
    """Rebuild the ranked pipeline from live signals (or the offline DEMO set). No CRM write."""
    body = body or {}
    build_pipeline(live=bool(body.get("live", True)))
    return {"ok": True, "action": "refresh", "n": len(_STATE["accounts"])}


def approve(body: dict | None = None) -> dict:
    """Human sign-off on a ranked account → queue it AND sync a real pilot to Twenty CRM."""
    acct = (body or {}).get("account", "")
    _STATE["approved"].add(acct)
    row = next((r for r in _STATE["accounts"] if r["account"] == acct), None)
    synced = twenty_sync(row) if row else False
    return {"ok": True, "action": "approve", "account": acct, "twenty_synced": synced}


def send_all(body: dict | None = None) -> dict:
    """Dispatch every approved outreach sequence to its prospect (the human-gated send)."""
    return {"ok": True, "action": "send_all", "sent": sorted(_STATE["approved"]),
            "note": "approved sequences dispatched (wire your sender to go live)"}
