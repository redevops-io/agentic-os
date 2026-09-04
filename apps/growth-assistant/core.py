"""growth-assistant core — the pure asset-store + multi-core (ERPNext / Listmonk / Postiz)
client layer, plus the deterministic action wrappers.

No web framework, no context-runtime: just stdlib + httpx against the three real OSS cores.
This is the layer the FastAPI app renders from AND the Mission Runtime operator invokes, so
the capability handlers can be tested against fake cores without booting the whole app.

Every generative action takes an optional `gen` callback — the LLM strategy generation lives
in `app.py` (which injects `_llm_json` / `_llm_text`). With `gen=None` (the operator's default)
each action falls back to a deterministic template, so a capability always produces output and
is exercisable without an LLM. Real core writes (ERPNext Leads / Listmonk lists / Postiz drafts)
happen only when `push` is set on the inputs and `GENERATION_ONLY` is off.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

# ── config (env; .env loaded idempotently so this module is self-sufficient when
# imported by the operator without the FastAPI app running its own loader first) ──
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

TENANT = os.environ.get("GROWTH_TENANT", "Meridian Wealth Management")
DATA_DIR = Path(os.environ.get("GROWTH_DATA_DIR", "/data"))
ASSET_DIR = DATA_DIR / "assets"
try:  # best-effort at import (prod: /data exists); tests / read-only FS must not crash import
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
except Exception:  # noqa: BLE001
    pass


def _ensure_asset_dir() -> None:
    """Make sure ASSET_DIR exists before a write (it may have been repointed post-import)."""
    try:
        ASSET_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass

ERPNEXT_URL = os.environ.get("ERPNEXT_URL", "http://localhost:8092").rstrip("/")
ERPNEXT_API_KEY = os.environ.get("ERPNEXT_API_KEY", "")
ERPNEXT_API_SECRET = os.environ.get("ERPNEXT_API_SECRET", "")
ERPNEXT_FRONT_URL = os.environ.get("ERPNEXT_FRONT_URL", "http://localhost:8092").rstrip("/")

LISTMONK_API_URL = os.environ.get("LISTMONK_API_URL", "http://localhost:9000").rstrip("/")
LISTMONK_API_USER = os.environ.get("LISTMONK_API_USER", "redevops-api")
LISTMONK_API_TOKEN = os.environ.get("LISTMONK_API_TOKEN", "")
LISTMONK_FRONT_URL = os.environ.get("LISTMONK_FRONT_URL", "http://localhost:9000").rstrip("/")

POSTIZ_API_URL = os.environ.get("POSTIZ_API_URL", "").rstrip("/")
POSTIZ_API_KEY = os.environ.get("POSTIZ_API_KEY", "")

# Public-surface safety: when true, NO action pushes to ERPNext/Listmonk/Postiz
# (the demo deployment behind demo.redevops.io sets this).
GENERATION_ONLY = os.environ.get("GENERATION_ONLY", "false").lower() in ("1", "true", "yes")

SUBTITLE = ("Strategic traction for first-time founders — subreddit incubation, founder-led "
            "growth, lead-magnet communities, and cold outreach — plus vetted-freelancer "
            "sourcing. Assets are drafted for a human to approve, never auto-published.")

# gen callbacks: JSON producer -> dict|list|None ; text producer -> str|None
GenJson = Callable[..., "dict | list | None"]
GenText = Callable[..., "str | None"]


def _push_on(body: dict) -> bool:
    """Whether an action may write to the cores. Forced OFF when GENERATION_ONLY
    (the public demo surface) — so a publicly-reachable instance never writes."""
    return bool(body.get("push")) and not GENERATION_ONLY


# --- startup profile helper --------------------------------------------------
def _startup(body: dict) -> dict:
    s = body.get("startup") or {}
    return {
        "name": s.get("name") or "the startup",
        "product": s.get("product") or "",
        "icp": s.get("icp") or "early adopters",
        "stage": s.get("stage") or "pre-launch",
        "problem": s.get("problem") or "",
        "founder_handle": s.get("founder_handle") or "",
        "links": s.get("links") or "",
    }


def _profile_block(s: dict) -> str:
    return (f"STARTUP: {s['name']}\nPRODUCT: {s['product']}\nICP (ideal customer): {s['icp']}\n"
            f"STAGE: {s['stage']}\nCORE PROBLEM IT SOLVES: {s['problem']}\n"
            f"FOUNDER HANDLE: {s['founder_handle']}\nLINKS: {s['links']}")


# --- asset store -------------------------------------------------------------
def _sid(v) -> str:
    """Normalize a viewer session id to a safe slug (or '' for none)."""
    return re.sub(r"[^A-Za-z0-9]", "", str(v or ""))[:32]


def _save_asset(atype: str, title: str, startup: dict, payload, session: str = "") -> dict:
    _ensure_asset_dir()
    aid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    asset = {"id": aid, "type": atype, "title": title, "startup": startup.get("name"),
             "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "session": _sid(session), "payload": payload, "pushed": {}}
    (ASSET_DIR / f"{aid}.json").write_text(json.dumps(asset, indent=2))
    return asset


def _load_assets() -> list[dict]:
    out = []
    for f in sorted(ASSET_DIR.glob("*.json"), reverse=True):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            continue
    return out


def _update_asset(aid: str, **fields) -> None:
    f = ASSET_DIR / f"{aid}.json"
    if f.exists():
        a = json.loads(f.read_text())
        a.update(fields)
        f.write_text(json.dumps(a, indent=2))


# --- core clients (all best-effort; degrade gracefully) ----------------------
def _erp_headers() -> dict:
    return {"Authorization": f"token {ERPNEXT_API_KEY}:{ERPNEXT_API_SECRET}",
            "Content-Type": "application/json"}


def erp_connected() -> bool:
    try:
        r = httpx.get(f"{ERPNEXT_URL}/api/resource/Lead", headers=_erp_headers(),
                      params={"limit_page_length": 1}, timeout=4.0)
        return r.status_code == 200
    except Exception:
        return False


def erp_create_lead(name: str, source_note: str, company: str = "", email: str = "") -> str | None:
    """Create an ERPNext Lead for an outreach target / first-customer prospect."""
    body = {"lead_name": name or "Prospect", "company_name": company or name or "",
            "source": "Campaign", "status": "Lead",
            "notes": f"\U0001f916 growth-assistant: {source_note}"}
    if email:
        body["email_id"] = email
    try:
        with httpx.Client(timeout=12.0) as c:
            r = c.post(ERPNEXT_URL + "/api/resource/Lead", headers=_erp_headers(), json=body)
            if r.status_code in (200, 201):
                return r.json().get("data", {}).get("name")
    except Exception:
        pass
    return None


def _lm_headers() -> dict:
    return {"Authorization": f"token {LISTMONK_API_USER}:{LISTMONK_API_TOKEN}",
            "Content-Type": "application/json"}


def listmonk_connected() -> bool:
    try:
        if httpx.get(f"{LISTMONK_API_URL}/health", timeout=4.0).status_code != 200:
            return False
        if not LISTMONK_API_TOKEN:
            return False
        return httpx.get(f"{LISTMONK_API_URL}/api/lists", headers=_lm_headers(),
                         params={"per_page": 1}, timeout=4.0).status_code == 200
    except Exception:
        return False


def listmonk_create_list(name: str, description: str = "") -> int | None:
    body = {"name": name[:200], "type": "public", "optin": "single",
            "tags": ["growth-assistant", "community"], "description": description[:300]}
    try:
        with httpx.Client(timeout=12.0) as c:
            r = c.post(LISTMONK_API_URL + "/api/lists", headers=_lm_headers(), json=body)
            if r.status_code in (200, 201):
                return r.json().get("data", {}).get("id")
    except Exception:
        pass
    return None


def postiz_reachable() -> bool:
    if not (POSTIZ_API_URL and POSTIZ_API_KEY):
        return False
    try:
        return httpx.get(POSTIZ_API_URL, timeout=3.0).status_code < 500
    except Exception:
        return False


def postiz_create_drafts(posts: list[str]) -> dict:
    """Best-effort: push founder-post drafts to Postiz's public API. Postiz often runs
    without its API port bound, so this degrades to a no-op and the drafts stay saved
    locally for the founder to paste/schedule by hand."""
    if not postiz_reachable():
        return {"ok": False, "reason": "Postiz API not reachable (drafts saved locally)"}
    created = 0
    for content in posts:
        try:
            r = httpx.post(POSTIZ_API_URL + "/public/v1/posts",
                           headers={"Authorization": POSTIZ_API_KEY, "Content-Type": "application/json"},
                           json={"type": "draft", "content": content}, timeout=10.0)
            if r.status_code < 300:
                created += 1
        except Exception:
            pass
    return {"ok": created > 0, "drafts_created": created, "of": len(posts)}


# --- freelancer search-link builder (no API; ToS-safe) -----------------------
ROLE_PRESETS = {
    "reddit_specialist": {
        "label": "Reddit community manager / subreddit mod",
        "keywords": "reddit community manager subreddit growth moderation",
        "vetting": ["Has grown/moderated a subreddit to 10k+ members",
                    "Can show karma + account age on relevant subs",
                    "Knows each sub's self-promo rules (the 9:1 rule)",
                    "Examples of non-spammy seeded threads that ranked",
                    "Understands shadowban / automod triggers"],
    },
    "copywriter": {
        "label": "Founder-voice ghostwriter (X threads + short-form)",
        "keywords": "twitter ghostwriter threads startup short-form copywriter linkedin",
        "vetting": ["Portfolio of high-hook X threads with real engagement",
                    "Can mirror a founder's voice from a short sample",
                    "Writes scroll-stopping first lines (the hook)",
                    "Comfortable with build-in-public / founder-led tone",
                    "Turnaround + revision policy is clear"],
    },
    "designer": {
        "label": "Tech-focused social/infographic designer",
        "keywords": "tech infographic designer social media templates quote cards saas",
        "vetting": ["Clean, modern tech aesthetic in portfolio",
                    "Can build a reusable template system (not one-offs)",
                    "Delivers source files (Figma) + variants",
                    "Understands data-viz / infographic clarity",
                    "On-brand color/type discipline"],
    },
}


def freelancer_links(keywords: str) -> dict:
    q = urllib.parse.quote_plus(keywords)
    qd = urllib.parse.quote_plus(keywords.replace(" ", "-"))
    return {
        "Upwork": f"https://www.upwork.com/nx/search/talent/?q={q}",
        "Fiverr": f"https://www.fiverr.com/search/gigs?query={q}",
        "Contra": f"https://contra.com/search?q={q}",
        "Wellfound": f"https://wellfound.com/search?q={q}",
        "PeoplePerHour": f"https://www.peopleperhour.com/freelance-{qd}-jobs",
        "Reddit r/forhire": f"https://www.reddit.com/r/forhire/search/?q={q}&restrict_sr=1&sort=new",
        "LinkedIn": f"https://www.linkedin.com/search/results/people/?keywords={q}",
    }


# --- deterministic templates (the gen=None fallback for each generative action) ---
def _tpl_playbook(s: dict) -> dict:
    return {
        "summary": (f"Zero-to-traction plan for {s['name']}: earn attention where the ICP "
                    f"({s['icp']}) already gathers, lead with the problem, and convert through "
                    "a founder-led narrative rather than paid spray-and-pray."),
        "subreddit_incubation": {
            "why": "Own a problem-first community the ICP searches for.",
            "first_moves": ["Reserve a brand subreddit", "Seed 10 discussion threads, no links"],
            "parallel_subs": ["r/startups", "r/SaaS"],
        },
        "founder_led_growth": {
            "platforms": ["x", "linkedin"],
            "voice": "build-in-public, specific, generous",
            "weekly_cadence": "5 posts + 1 thread",
            "sample_hooks": ["What I learned shipping v1", "The mistake that cost us a month"],
        },
        "lead_magnet_community": {
            "platform": "discord",
            "angle": "problem not product",
            "seeding_loop": "invite every helpful commenter to a focused space",
        },
        "cold_outreach": {
            "audit_loom_angle": "60s audit of a recent launcher's community setup",
            "accelerator_play": "offer a free Community-101 workshop for referrals",
        },
        "thirty_day_milestones": ["week1 seed the subreddit", "week2 first 10 founder posts",
                                  "week3 open the community", "week4 first cold-outreach batch"],
        "north_star_metric": "weekly engaged community members",
    }


def _tpl_subreddit(s: dict) -> dict:
    return {
        "subreddit_name_ideas": [f"r/{re.sub(r'[^A-Za-z0-9]', '', s['name']) or 'brand'}",
                                 "r/" + (s['icp'].split()[0].title() if s['icp'] else 'Builders')],
        "positioning": "problem-first, not product-first",
        "setup_checklist": ["Write the sidebar around the ICP's problem", "Set flair for post types"],
        "automod_and_rules": ["No links in first comment", "Enforce the 9:1 give/promo rule"],
        "first_100_threads": [
            {"title": f"How do you currently handle {s['problem'] or 'this problem'}?",
             "angle": "listen", "format": "discussion"},
            {"title": "Weekly wins thread", "angle": "community", "format": "discussion"},
            {"title": f"A field guide to {s['icp'] or 'the space'}", "angle": "value", "format": "guide"},
        ],
        "parallel_subreddits": [{"sub": "r/startups", "why": "adjacent ICP",
                                 "engagement_rule": "give value, no links first"}],
        "thirty_day_cadence": "3 seeded threads/week + reply to every comment",
    }


def _tpl_founder_content(s: dict, platform: str, count: int) -> dict:
    posts = [{"hook": f"Day {i + 1} building {s['name']}:",
              "body": f"What we learned about {s['problem'] or 'the problem'} today.",
              "format": "single", "cta": "Follow the build."} for i in range(count)]
    return {"voice_profile": "founder, build-in-public, specific and honest",
            "content_pillars": ["the problem", "the journey", "the lessons"],
            "posts": posts}


def _tpl_community(s: dict, platform: str) -> dict:
    return {"community_name": f"{s['name']} community",
            "problem_promise": f"A space to solve {s['problem'] or 'the core problem'} together.",
            "channel_structure": ["#intros", "#wins", "#help"],
            "onboarding_flow": ["welcome DM", "pin the problem-promise"],
            "seeding_loop": "invite helpful commenters from the subreddit + posts",
            "first_week_events": ["kickoff AMA", "office hours"],
            "conversion_path": "members hit the problem, see the product referenced by peers, opt in"}


def _tpl_cold_outreach(s: dict) -> dict:
    return {"loom_audit_script": f"30-60s audit of a recent launcher's community setup for {s['icp']}.",
            "three_quick_wins": ["seed one subreddit thread", "add a problem-first pinned post",
                                 "DM 5 early users for feedback"],
            "accelerator_workshop_pitch": "offer a free Community-101 workshop to a cohort for referrals",
            "dm_templates": {"x": "Saw your launch — quick idea to get your first 100 users…",
                             "linkedin": "Congrats on the launch. I put together a short audit…"}}


def _tpl_hire_brief(s: dict, label: str) -> dict:
    return {"job_title": label, "scope_summary": f"Help {s['name']} execute its growth plan.",
            "deliverables": ["weekly output", "a monthly report"],
            "must_haves": ["relevant portfolio", "clear communication"],
            "red_flags": ["no verifiable work", "generic pitch"],
            "trial_task": "a small paid test to vet them",
            "outreach_dm": f"Hi — building {s['name']}, looking for a {label}. Open to a small paid trial?",
            "pricing": {"upwork_hourly_usd": "25-60", "upwork_project_usd": "500-2000",
                        "fiverr_gig_usd": "50-500", "typical_monthly_retainer_usd": "800-2500",
                        "notes": "range depends on seniority, niche fit, and turnaround"}}


# --- agentic actions ---------------------------------------------------------
# Each takes an optional `gen` LLM callback (injected from app.py). gen=None -> a
# deterministic template, so the capability is exercisable without an LLM.
def playbook(body: dict, gen: GenJson | None = None) -> dict:
    s = _startup(body)
    if gen is None:
        plan = _tpl_playbook(s)
    else:
        plan = gen(
            "You are a startup growth strategist (think Demand Curve / GrowthRocks) building a "
            "zero-to-traction plan for a first-time founder. Across FOUR pillars, give concrete, "
            "non-generic, non-spammy actions. JSON schema:\n"
            '{"summary":"2-3 sentence thesis",'
            '"subreddit_incubation":{"why":"","first_moves":["",""],"parallel_subs":["",""]},'
            '"founder_led_growth":{"platforms":["x","linkedin"],"voice":"","weekly_cadence":"","sample_hooks":["",""]},'
            '"lead_magnet_community":{"platform":"discord|whatsapp|facebook","angle":"problem not product","seeding_loop":""},'
            '"cold_outreach":{"audit_loom_angle":"","accelerator_play":""},'
            '"thirty_day_milestones":["week1 ...","week2 ...","week3 ...","week4 ..."],'
            '"north_star_metric":""}\n\n' + _profile_block(s), max_tokens=2400)
        if not plan:
            return {"status": "error", "action": "playbook", "error": "brain unavailable"}
    asset = _save_asset("playbook", f"Growth playbook — {s['name']}", s, plan, body.get("session", ""))
    return {"status": "done", "action": "playbook", "asset_id": asset["id"], "playbook": plan}


def subreddit_plan(body: dict, gen: GenJson | None = None) -> dict:
    s = _startup(body)
    if gen is None:
        plan = _tpl_subreddit(s)
    else:
        plan = gen(
            "You are a Reddit growth specialist. Design a brand-subreddit incubation plan that "
            "respects Reddit culture and self-promo rules. JSON schema:\n"
            '{"subreddit_name_ideas":["r/...","r/..."],"positioning":"problem-first, not product-first",'
            '"setup_checklist":["",""],"automod_and_rules":["",""],'
            '"first_100_threads":[{"title":"","angle":"","format":"discussion|guide|AMA|poll"}],'
            '"parallel_subreddits":[{"sub":"r/...","why":"","engagement_rule":"give value, no links first"}],'
            '"thirty_day_cadence":""}\n'
            "Give at least 12 seed threads in first_100_threads (a representative starter set).\n\n"
            + _profile_block(s), max_tokens=2600)
        if not plan:
            return {"status": "error", "action": "subreddit_plan", "error": "brain unavailable"}
    asset = _save_asset("subreddit_plan", f"Subreddit incubation — {s['name']}", s, plan, body.get("session", ""))
    return {"status": "done", "action": "subreddit_plan", "asset_id": asset["id"], "plan": plan}


def founder_content(body: dict, gen: GenJson | None = None) -> dict:
    s = _startup(body)
    platform = (body.get("platform") or "x").lower()
    count = max(1, min(int(body.get("count", 7) or 7), 15))
    plat_label = "X (Twitter)" if platform in ("x", "twitter") else "LinkedIn"
    if gen is None:
        out = _tpl_founder_content(s, platform, count)
    else:
        out = gen(
            f"You are a founder-led growth ghostwriter. Build a {plat_label} content set in the "
            f"founder's voice (build-in-public, story-first, audience buys the person before the "
            f"company). JSON schema:\n"
            '{"voice_profile":"","content_pillars":["",""],'
            f'"posts":[{{"hook":"","body":"","format":"thread|single|carousel","cta":""}}]}}\n'
            f"Produce exactly {count} posts tuned for {plat_label}.\n\n" + _profile_block(s),
            max_tokens=2400)
        if not out:
            return {"status": "error", "action": "founder_content", "error": "brain unavailable"}
    asset = _save_asset("founder_content", f"{plat_label} content — {s['name']}", s,
                        {"platform": platform, **out}, body.get("session", ""))
    result = {"status": "done", "action": "founder_content", "asset_id": asset["id"],
              "platform": platform, "content": out}
    if _push_on(body):
        drafts = [f"{p.get('hook', '')}\n\n{p.get('body', '')}".strip()
                  for p in (out.get("posts") or [])]
        push = postiz_create_drafts(drafts)
        _update_asset(asset["id"], pushed={"postiz": push})
        result["pushed"] = {"postiz": push}
    return result


def community_blueprint(body: dict, gen: GenJson | None = None) -> dict:
    s = _startup(body)
    platform = (body.get("platform") or "discord").lower()
    if gen is None:
        bp = _tpl_community(s, platform)
    else:
        bp = gen(
            f"You are a community architect. Design a lead-magnet community on {platform} centered on "
            "the PROBLEM (not the product), to attract the ICP and convert to first customers. JSON:\n"
            '{"community_name":"","problem_promise":"","channel_structure":["",""],'
            '"onboarding_flow":["",""],"seeding_loop":"","first_week_events":["",""],'
            '"conversion_path":"how members become customers without being sold to"}\n\n'
            + _profile_block(s), max_tokens=1200)
        if not bp:
            return {"status": "error", "action": "community_blueprint", "error": "brain unavailable"}
    asset = _save_asset("community_blueprint", f"{platform.title()} community — {s['name']}", s,
                        {"platform": platform, **bp}, body.get("session", ""))
    result = {"status": "done", "action": "community_blueprint", "asset_id": asset["id"],
              "platform": platform, "blueprint": bp}
    if _push_on(body):
        lid = listmonk_create_list(bp.get("community_name") or f"{s['name']} community",
                                   bp.get("problem_promise", ""))
        push = {"ok": lid is not None, "listmonk_list_id": lid}
        _update_asset(asset["id"], pushed={"listmonk": push})
        result["pushed"] = {"listmonk": push}
    return result


def cold_outreach(body: dict, gen: GenJson | None = None) -> dict:
    s = _startup(body)
    targets = body.get("targets") or []
    if gen is None:
        out = _tpl_cold_outreach(s)
    else:
        out = gen(
            "You are an early-stage growth operator. Build an audit-based cold outreach kit aimed at "
            "first-time founders who just launched (e.g. on Product Hunt). JSON:\n"
            '{"loom_audit_script":"30-60s script giving 3 actionable community tips",'
            '"three_quick_wins":["","",""],'
            '"accelerator_workshop_pitch":"offer a free Community-101 workshop to a cohort for referrals",'
            '"dm_templates":{"x":"","linkedin":""}}\n\n' + _profile_block(s), max_tokens=1500)
        if not out:
            return {"status": "error", "action": "cold_outreach", "error": "brain unavailable"}
    asset = _save_asset("cold_outreach", f"Cold outreach kit — {s['name']}", s,
                        {"kit": out, "targets": targets}, body.get("session", ""))
    result = {"status": "done", "action": "cold_outreach", "asset_id": asset["id"], "kit": out}
    if _push_on(body) and targets:
        created = []
        for t in targets[:50]:
            nm = t.get("name") or t.get("handle") or "Prospect"
            lead = erp_create_lead(nm, f"cold-outreach target ({t.get('handle', '')} {t.get('note', '')})",
                                   company=t.get("company", ""), email=t.get("email", ""))
            if lead:
                created.append(lead)
        push = {"ok": bool(created), "leads_created": created, "of": len(targets)}
        _update_asset(asset["id"], pushed={"erpnext": push})
        result["pushed"] = {"erpnext": push}
    return result


def hire_brief(body: dict, gen: GenJson | None = None) -> dict:
    role = (body.get("role") or "reddit_specialist").lower()
    preset = ROLE_PRESETS.get(role)
    if not preset:
        return {"status": "error", "action": "hire_brief",
                "error": f"unknown role '{role}'", "supported": list(ROLE_PRESETS)}
    s = _startup(body)
    extra = body.get("keywords") or ""
    keywords = (preset["keywords"] + " " + extra).strip()
    if gen is None:
        jd = _tpl_hire_brief(s, preset["label"])
    else:
        jd = gen(
            f"Write a freelancer hiring brief for a {preset['label']} to help the startup below, "
            "INCLUDING rough market pricing. JSON:\n"
            '{"job_title":"","scope_summary":"","deliverables":["",""],'
            '"must_haves":["",""],"red_flags":["",""],"trial_task":"a small paid test to vet them",'
            '"outreach_dm":"a short, specific DM to send a candidate",'
            '"pricing":{"upwork_hourly_usd":"e.g. 25-60","upwork_project_usd":"e.g. 500-2000",'
            '"fiverr_gig_usd":"e.g. 50-500","typical_monthly_retainer_usd":"e.g. 800-2500",'
            '"notes":"what drives the range up/down"}}\n\n' + _profile_block(s),
            max_tokens=1100) or {}
    payload = {"role": role, "role_label": preset["label"],
               "vetting_scorecard": preset["vetting"], "brief": jd,
               "pricing": jd.get("pricing", {}),
               "search_links": freelancer_links(keywords),
               "note": ("No platform has a clean public API for live talent search (Fiverr none; "
                        "Upwork needs an approved app), so these links run the search for you and the "
                        "pricing is a ROUGH market estimate (not a live quote) — verify on-platform. "
                        "Vet against the scorecard and send the outreach DM.")}
    asset = _save_asset("hire_brief", f"Hire: {preset['label']} — {s['name']}", s, payload,
                        body.get("session", ""))
    return {"status": "done", "action": "hire_brief", "asset_id": asset["id"], **payload}


def ask(body: dict, gen: GenText | None = None) -> dict:
    q = (body.get("q") or "").strip()
    if not q:
        return {"status": "error", "action": "ask", "error": "q (question) required"}
    assets = _load_assets()
    idx = [{"id": a["id"], "type": a["type"], "title": a["title"],
            "startup": a.get("startup"), "created": a["created"]} for a in assets[:40]]
    cores = {"erpnext": erp_connected(), "listmonk": listmonk_connected(),
             "postiz": postiz_reachable()}
    ctx = {"asset_count": len(assets), "assets": idx, "cores": cores}
    if gen is None:
        wired = ", ".join(k for k, v in cores.items() if v) or "none"
        out = (f"{len(assets)} growth asset(s) on file. Cores wired: {wired}. "
               f"(Deterministic snapshot — no LLM brain attached to answer '{q}'.)")
    else:
        out = gen(
            "You are a growth-program manager. Answer using ONLY this snapshot of generated assets + "
            f"core connectivity. Be concise.\n\nSNAPSHOT:\n{json.dumps(ctx)[:3500]}\n\nQUESTION: {q}",
            max_tokens=500)
    return {"status": "done", "action": "ask", "q": q, "answer": out or "(brain unavailable)"}


# --- live data + KPIs --------------------------------------------------------
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 10.0


def fetch_activity(force: bool = False, session: str = "") -> dict:
    now = time.time()
    sid = _sid(session)
    if not sid and not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]
    assets = _load_assets()
    if sid:
        # per-viewer isolation: only this session's assets
        assets = [a for a in assets if _sid(a.get("session")) == sid]
    by_type: dict[str, int] = {}
    for a in assets:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1
    leads_pushed = sum(len(((a.get("pushed") or {}).get("erpnext") or {}).get("leads_created", []) or [])
                       for a in assets)
    lists_pushed = sum(1 for a in assets
                       if ((a.get("pushed") or {}).get("listmonk") or {}).get("ok"))
    cores = {"erpnext": erp_connected(), "listmonk": listmonk_connected(), "postiz": postiz_reachable()}
    recent = [{"id": a["id"], "type": a["type"], "title": a["title"],
               "startup": a.get("startup") or "—", "created": a["created"],
               "pushed": ", ".join(k for k in (a.get("pushed") or {})) or "—"}
              for a in assets[:25]]
    data = {
        "tenant": TENANT, "connected": any(cores.values()), "cores": cores,
        "erp_front": ERPNEXT_FRONT_URL, "listmonk_front": LISTMONK_FRONT_URL,
        "kpis": [
            {"label": "Growth assets", "value": str(len(assets)),
             "note": " · ".join(f"{v} {k.replace('_', ' ')}" for k, v in by_type.items()) or "none yet"},
            {"label": "Leads → CRM", "value": str(leads_pushed), "note": "first-customer prospects"},
            {"label": "Community lists", "value": str(lists_pushed), "note": "seeded in Listmonk"},
            {"label": "Cores wired", "value": str(sum(cores.values())) + "/3",
             "note": "ERPNext · Listmonk · Postiz"},
        ],
        "by_type": by_type, "recent": recent,
    }
    if not sid:
        _CACHE.update(ts=now, data=data)
    return data
