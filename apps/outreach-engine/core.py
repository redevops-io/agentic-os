"""agentic-outreach-engine core — the pure Twenty CRM client + agentic actions.

No web framework, no context-runtime, no LLM: just stdlib urllib against a real Twenty CRM
core (plus the public GitHub/HN signal sources). This is the layer the FastAPI app renders
from AND the Mission Runtime operator invokes, so the capability handlers can be tested
against a fake Twenty without booting the whole console.

`approve` and `send_all` are APPROVAL-GATED INSIDE this module (staged `pending_approval` unless a
runtime-delivered `_approval` is present). The LLM-driven pieces (lead-discovery planning, follow-up drafting, the conversational
router) stay in `app.py` — this module is dependency-light and deterministic so the three
`/agent/run` actions (refresh / approve / send_all) run identically in-process, over HTTP,
and under test.
"""
from __future__ import annotations

import base64
import hmac
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request

from agentic_os.mission.operator_sdk import pending_approval
from agentic_os.world import enrichment as _enrichment

# ── config (env) ─────────────────────────────────────────────────────────────
TWENTY_URL = os.environ.get("TWENTY_URL", "http://twenty:3000").rstrip("/")
TWENTY_API_KEY = os.environ.get("TWENTY_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Public Twenty origin (browsable UI) — read from env so the read-back panel can deep-link a
# synced opportunity into the real CRM over https. Falls back to the internal URL.
TWENTY_PUBLIC_URL = os.environ.get("TWENTY_PUBLIC_URL", "https://crm.redevops.io").rstrip("/")

# ── the real sender for send_all: a Gmail mailbox's App Password (smtp.gmail.com, STARTTLS). No
# Postmark/SES wiring here on purpose -- outreach-engine is a generic, OUTREACH_TENANT-configurable
# app, and the simplest honest sender for an arbitrary already-owned mailbox is SMTP + an app password.
GMAIL_SMTP_USER = os.environ.get("GMAIL_SMTP_USER", "")
GMAIL_SMTP_APP_PASSWORD = os.environ.get("GMAIL_SMTP_APP_PASSWORD", "")

# ── tenant / product framing — configurable so the CRM agent works for ANY workspace. Defaults to
# Meridian Wealth Management (the demo company used across the agentic apps).
TENANT = os.environ.get("OUTREACH_TENANT", "Meridian Wealth Management")
PITCH = os.environ.get("OUTREACH_PITCH",
    "we offer fee-only financial planning, portfolio management and tax-aware retirement investing — a fiduciary advisory team that puts your goals first")

# ── the play policy: a FIXED, hand-set mapping (signal -> play). It is NOT learned from outcomes — there is no
# bandit/feedback loop behind it. Surfaced in outputs as PLAY_POLICY so no text claims otherwise. ──
PLAY_POLICY = "fixed policy (not learned)"
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
    {"account": "Whitfield Family Trust", "signal": "cold", "source": "manual",
     "evidence": "manages a multi-generational trust across several accounts — ongoing portfolio oversight and estate planning"},
    {"account": "Okonkwo Holdings", "signal": "cold", "source": "manual",
     "evidence": "founder with a concentrated stock position after a recent liquidity event — exploring diversification and tax-aware planning"},
    {"account": "Delgado Retirement", "signal": "cold", "source": "manual",
     "evidence": "household nearing retirement with a $2M 401(k) rollover and RMD planning ahead"},
]

_STATE = {"accounts": [], "approved": set(), "candidates": []}   # in-memory pipeline, approval set, discovered candidates


# ── operator gate (route level) ──────────────────────────────────────────────
# The side-effecting core actions below STAGE (pending_approval, executed False) unless the input carries a
# runtime-delivered ``_approval``. The unauthenticated HTTP routes strip any caller-supplied ``_approval*`` and
# only INJECT one server-side when the request carries the valid operator Basic credential (env
# OPERATOR_BASIC_AUTH, or OUTREACH_BASIC_AUTH, as ``user:pass``). Unset/malformed credential => nobody is the
# operator (fail closed). LLM / console tool calls never pass through here, so they can never be approved.
def _operator_credential() -> str:
    combo = os.environ.get("OPERATOR_BASIC_AUTH") or os.environ.get("OUTREACH_BASIC_AUTH") or ""
    user, sep, pw = combo.partition(":")
    return combo if (sep and user and pw) else ""


def _ctx_marker() -> "dict | None":
    """The marker this request earned from an authenticated context via the route-authorization layer (never the body)."""
    from agentic_os.app_kit.route_authz import current_marker
    return current_marker()


def operator_approval(authorization: "str | None") -> "dict | None":
    """The ``_approval`` marker for a request whose ``Authorization`` header is the valid operator Basic
    credential, else None (unauthenticated / wrong credential / no credential configured)."""
    expected = _operator_credential()
    if not expected or not authorization or authorization[:6].lower() != "basic ":
        return _ctx_marker()
    try:
        got = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
    except Exception:  # noqa: BLE001
        return _ctx_marker()
    if hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8")):
        return {"approved": True, "by": "operator-basic"}
    return _ctx_marker()


def with_operator_approval(body: dict, authorization: "str | None") -> dict:
    """``body`` plus the operator marker when (and only when) the credential is valid."""
    appr = operator_approval(authorization)
    return {**body, "_approval": appr} if appr else body


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


_DEV_SOURCES = ("github", "hn", "hn_hiring")
_FIN_WORDS = ("portfolio", "retire", "401", "wealth", "estate", "trust", "tax", "invest", "liquidity", "rmd")


def icp_of(s: dict) -> str:
    """Lead ICP: explicit ``icp`` wins; developer for GitHub/HN-sourced or tech-pain signals; finance for
    finance-flavoured evidence; otherwise 'unknown' (neutral opener)."""
    icp = str(s.get("icp") or "").lower()
    if icp in ("developer", "finance"):
        return icp
    if s.get("source") in _DEV_SOURCES or s.get("signal") == "tech_pain":
        return "developer"
    ev = str(s.get("evidence") or "").lower()
    if any(w in ev for w in _FIN_WORDS):
        return "finance"
    return "unknown"


def neutral_followup(name: str = "") -> str:
    return ("Hi — just following up on my earlier note" + (f" about {name}" if name else "") + ". "
            "Happy to answer any questions or share more detail — would a short call this week be useful?")


def teardown(s: dict) -> dict:
    a = s["account"]
    icp = icp_of(s)
    if icp == "finance":
        body = ("Hi — I came across " + a + " and wanted to reach out. At " + TENANT + ", " + PITCH + ".\n\n"
                "If " + a + " is weighing options, I'd be glad to put together a complimentary, no-obligation portfolio review and "
                "answer any questions.\n\nWorth a short call this week?")
    elif icp == "developer":
        hook = HOOK.get(s.get("signal"), HOOK["cold"]).format(a=a)
        body = ("Hi — " + hook + ". I'm reaching out from " + TENANT + " with no pitch attached: if it would be "
                "useful to compare notes, reply and I'll send a couple of times for a short call.")
    else:
        body = ("Hi — I came across " + a + " and wanted to reach out from " + TENANT + ". If it's relevant, I'd be "
                "glad to share more and answer any questions.\n\nWould a short call this week be useful?")
    return {"subject": a + ": a quick note from " + TENANT, "body": body, "icp": icp}


def pitch_visual_html(row: dict) -> str:
    """A small, honestly-labelled visualization of the REAL evidence that produced this outreach — not a
    vanity chart. Every bar is a number already on the row (signal/source weight, and real GitHub stars
    when present); nothing here is invented for the prospect. Every style is inlined (no external CSS,
    no <style> block) because that's what actually survives into an email client's DOM.
    """
    bars = [("Signal strength", SIGNAL_PRIORITY.get(row.get("signal"), 0.15)),
            ("Source confidence", SOURCE_WEIGHT.get(row.get("source"), 0.5))]
    if row.get("stars"):
        bars.append(("Project traction (★)", min(1.0, row["stars"] / 5000.0)))
    rows = ""
    for label, frac in bars:
        pct = max(4, round(frac * 100))
        rows += (
            "<tr>"
            "<td style=\"padding:4px 10px 4px 0;font:13px -apple-system,Segoe UI,sans-serif;color:#333;"
            "white-space:nowrap\">" + html.escape(label) + "</td>"
            "<td style=\"width:220px\"><div style=\"background:#e7e9ee;border-radius:4px;height:10px;"
            "width:220px\"><div style=\"background:#1f5eff;border-radius:4px;height:10px;width:" +
            str(pct) + "%\"></div></div></td>"
            "<td style=\"padding:4px 0 4px 10px;font:12px -apple-system,Segoe UI,sans-serif;color:#667\">" +
            str(round(frac * 100)) + "%</td></tr>"
        )
    return (
        "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" style=\"margin:14px 0;"
        "border-collapse:collapse\">"
        "<tr><td colspan=\"3\" style=\"font:600 12px -apple-system,Segoe UI,sans-serif;color:#667;"
        "padding-bottom:6px\">WHY WE REACHED OUT &mdash; the actual evidence, not a guess</td></tr>" +
        rows + "</table>"
    )


def render_pitch_html(subject: str, text_body: str, row: dict) -> str:
    """The HTML alternative for an outreach email: the drafted text, the evidence visualization, and the
    lead's own real link (its GitHub repo/source, when the row has one) — never a fabricated one."""
    paras = "".join(f"<p style=\"margin:0 0 12px;font:14px -apple-system,Segoe UI,sans-serif;color:#1a1a1a;"
                    f"line-height:1.5\">{html.escape(p)}</p>" for p in text_body.strip().split("\n\n") if p.strip())
    link = row.get("url") or row.get("artifact")
    link_html = (f"<p style=\"margin:12px 0 0\"><a href=\"{html.escape(link)}\" "
                f"style=\"color:#1f5eff;text-decoration:none\">{html.escape(link)}</a></p>") if link else ""
    return (f"<div style=\"max-width:560px\">{paras}{pitch_visual_html(row)}{link_html}</div>")


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
    raw = list(raw) + list(_STATE["candidates"])      # discovered candidates stay in the pipeline until approved
    seen, rows = set(), []
    for s in sorted(raw, key=score, reverse=True):
        k = (s["account"].lower(), s["signal"])
        if k in seen:
            continue
        seen.add(k)
        rows.append({**s, "score": score(s), "play": PLAY.get(s["signal"], PLAY["cold"]),
                     "draft": teardown(s), "approved": s["account"] in _STATE["approved"],
                     "status": ("approved" if s["account"] in _STATE["approved"]
                                else "candidate" if s.get("candidate") else "ranked"),
                     "play_policy": PLAY_POLICY})
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


def _domain_of(owner: dict) -> str:
    """A bare company domain (no scheme, no leading ``www.``) from a GitHub profile's ``blog`` field,
    or '' if the profile has none / it doesn't parse as a host."""
    blog = (owner.get("blog") or "").strip()
    if not blog:
        return ""
    url = blog if "://" in blog else "https://" + blog
    try:
        host = urllib.parse.urlparse(url).netloc
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def enrich_contact(row: dict, *, provider=None) -> dict:
    """Resolve a verified business email for this lead's point of contact via the configured enrichment
    provider (Hunter by default; ``HUNTER_API_KEY``). Spends the provider's credits — call per-account on
    demand, never on every pipeline refresh. Needs a real company domain, taken from the lead's public
    GitHub profile; leads with no discoverable domain (e.g. the offline DEMO set, which has no GitHub
    presence) stay at email=None — this never fabricates an address."""
    acct = row.get("account", "")
    full = row.get("full_name") or ""
    owner = _gh_profile(acct) if row.get("source") == "github" else {}
    poc = _gh_top_contributor(full) if full else {}
    domain = _domain_of(owner)
    if not domain:
        return {"email": None, "verified": False, "provider": None,
                "reason": "no company domain available for this lead"}
    name = poc.get("name") or owner.get("name") or acct
    parts = str(name).split(" ", 1)
    first, last = parts[0], (parts[1] if len(parts) > 1 else "")
    return _enrichment.resolve_verified_email(domain=domain, first_name=first, last_name=last, provider=provider)


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


def enrich(body: dict | None = None) -> dict:
    """Resolve + attach a verified business email for one ranked account (read-only; no CRM write, no
    approval gate — it spends the enrichment provider's credits, not the prospect's trust). Call this
    before ``approve``/``send_all`` so the sequence has a real address to land on."""
    body = body or {}
    acct = body.get("account", "")
    row = next((r for r in _STATE["accounts"] if r["account"] == acct), None)
    if row is None:
        return {"ok": False, "action": "enrich", "account": acct,
                "reason": "unknown account — refresh the pipeline first"}
    result = enrich_contact(row)
    row["contact_email"] = result.get("email")
    row["email_verified"] = bool(result.get("verified"))
    row["email_provider"] = result.get("provider")
    if result.get("reason"):
        row["email_reason"] = result["reason"]
    return {"ok": True, "action": "enrich", "account": acct, **result}


def approve(body: dict | None = None) -> dict:
    """Human sign-off on a ranked account → queue it AND sync a real pilot to Twenty CRM."""
    body = body or {}
    if not body.get("_approval"):          # the gate lives HERE so operator, routes and console tools are all covered
        return pending_approval("outreach.approve", account=str(body.get("account", ""))[:200], lead_synced=False)
    acct = body.get("account", "")
    _STATE["approved"].add(acct)
    row = next((r for r in _STATE["accounts"] if r["account"] == acct), None)
    synced = twenty_sync(row) if row else False
    return {"ok": True, "action": "approve", "account": acct, "twenty_synced": synced}


def _sender():
    """The configured real sender (Gmail SMTP app-password), or None when it isn't wired."""
    if GMAIL_SMTP_USER and GMAIL_SMTP_APP_PASSWORD:
        from agentic_os.world.connectors import GmailSMTPConnector
        return GmailSMTPConnector(GMAIL_SMTP_USER, GMAIL_SMTP_APP_PASSWORD)
    return None


def send_all(body: dict | None = None) -> dict:
    """Dispatch every approved outreach sequence to its prospect (the human-gated send).

    Sends ONLY to an approved account that `enrich` already resolved a VERIFIED contact_email for; an
    approved account with no verified email is reported as skipped, never guessed at. With no sender
    (GMAIL_SMTP_USER/GMAIL_SMTP_APP_PASSWORD) configured, this is an honest no-op exactly as before:
    `sequences_sent: False` is set explicitly so the operator SDK's output-marker merge can't read this
    back as a successful send (N4) when nothing actually reached a prospect."""
    if not (body or {}).get("_approval"):
        return pending_approval("outreach.send_all", staged=sorted(_STATE["approved"]), sequences_sent=False)
    approved = sorted(_STATE["approved"])
    sender = _sender()
    if sender is None:
        return {"ok": False, "action": "send_all", "status": "not_configured", "executed": False,
                "sequences_sent": False, "dispatched": 0, "staged": approved, "sender_configured": False,
                "reason": "no sender (SMTP/provider) is wired into this deployment",
                "note": "no live sender configured — approved sequences are staged, not sent. "
                        "Wire a sender (SMTP/provider) to actually dispatch."}
    results = []
    sent = 0
    for acct in approved:
        row = next((r for r in _STATE["accounts"] if r["account"] == acct), None)
        to = (row or {}).get("contact_email")
        if not row or not to or not row.get("email_verified"):
            results.append({"account": acct, "sent": False,
                            "reason": "no verified contact email — run `enrich` for this account first"})
            continue
        draft = row.get("draft") or teardown(row)
        html_body = draft.get("html") or render_pitch_html(draft["subject"], draft["body"], row)
        r = sender.send(to=to, subject=draft["subject"], body=draft["body"], tag="outreach-engine",
                        html=html_body)
        ok = r.get("error_code", 1) == 0
        entry = {"account": acct, "sent": ok, "to": to, "message_id": r.get("message_id")}
        if not ok:
            entry["reason"] = r.get("error", "send failed")
        results.append(entry)
        sent += int(ok)
    return {"ok": True, "action": "send_all", "status": "dispatched" if sent else "no_eligible_recipients",
            "executed": True, "sequences_sent": sent > 0, "dispatched": sent, "staged": approved,
            "sender_configured": True, "results": results}
