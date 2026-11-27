"""vibexgen adapter — brings vibexgen.io's content capabilities to the social agent.

vibexgen.io is redevops' AI content platform. Its API (the `mcp-api-server`) exposes trend
discovery (yt-dlp search across YouTube), an NL video agent (`/chat`), and a video toolchain
(FFmpeg convert/trim/resize/gif + auto-edit). This adapter surfaces a demo-limited slice to
Social Autopilot so the agent can decide *what* to post (trend intelligence) and *make* the media.

It is **config-driven and gracefully degrading**, exactly like the CRM / enrichment adapters:
  - Set VIBEXGEN_API_URL + an authenticated VIBEXGEN_API_KEY (+ optional VIBEXGEN_API_HOST for
    vhost routing) and calls go LIVE against the real platform. Live mode is key-gated on purpose
    so a public demo never rides the platform's unauthenticated internal path (see _LIVE below).
  - No key (the public demo) / unreachable → a curated demo dataset for the Meridian Wealth niche,
    so the feature still works without the platform. `status()` reports which mode is active.

In the demo we deliberately expose only two low-cost, read-mostly capabilities: trend discovery
(real YouTube search) and a generation *request* routed through the vibexgen video agent. Heavy
GPU generation, TTS and file downloads stay gated behind the platform's own auth.
"""
from __future__ import annotations

import json
import os

import httpx

VIBEXGEN_API_URL = os.environ.get("VIBEXGEN_API_URL", "").rstrip("/")
VIBEXGEN_API_KEY = os.environ.get("VIBEXGEN_API_KEY", "")
# nginx on the vibexgen host routes by Host header (e.g. api.vibexgen.io). When the URL is an
# internal IP we still need the public vhost so the request lands on the right upstream.
VIBEXGEN_API_HOST = os.environ.get("VIBEXGEN_API_HOST", "")
NICHE = os.environ.get("VIBEXGEN_NICHE", "wealth management & financial planning")

# Live mode is CONFIG-GATED and requires an AUTHENTICATED key. The vibexgen API also answers
# unauthenticated calls on its internal network path — but a public demo must never ride that
# anonymous connection, so we only go live when a real VIBEXGEN_API_KEY (a `vxg_…` bearer) is
# configured. A trusted / non-public deployment that deliberately wants the keyless internal
# path can opt in explicitly with VIBEXGEN_ALLOW_ANON=1. Default (and the public demo): OFF.
VIBEXGEN_ALLOW_ANON = os.environ.get("VIBEXGEN_ALLOW_ANON", "") == "1"
_LIVE = bool(VIBEXGEN_API_URL) and (bool(VIBEXGEN_API_KEY) or VIBEXGEN_ALLOW_ANON)

# --- curated demo intelligence (used when vibexgen isn't connected) ----------
_DEMO_HASHTAGS = ["#WealthManagement", "#RetirementPlanning", "#TaxPlanning", "#MarketCommentary",
                  "#FinancialPlanning", "#Investing", "#FiduciaryAdvice", "#PersonalFinance"]
_DEMO_ANGLES = [
    {"format": "Weekly market recap explainer", "hook": "What moved markets this week — in 60 seconds",
     "why": "timely explainers over-index on saves + shares"},
    {"format": "Tax-loss harvesting walkthrough", "hook": "How to turn a down year into a tax break",
     "why": "actionable how-tos drive comments and DMs"},
    {"format": "Whiteboard: the power of compounding", "hook": "The chart that explains why starting early wins",
     "why": "simple visuals = high watch-time, low words"},
    {"format": "Fee-transparency explainer", "hook": "What our advisory fee actually pays for",
     "why": "fee transparency builds trust + follows"},
    {"format": "5 questions before you retire", "hook": "#3 surprises every pre-retiree",
     "why": "listicle hooks are the reliable reach play"},
]


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if VIBEXGEN_API_HOST:
        h["Host"] = VIBEXGEN_API_HOST
    if VIBEXGEN_API_KEY:
        h["Authorization"] = f"Bearer {VIBEXGEN_API_KEY}"
    return h


def connected() -> bool:
    """True iff live mode is config-enabled (URL + authenticated key) AND health answers."""
    if not _LIVE:
        return False
    try:
        r = httpx.get(f"{VIBEXGEN_API_URL}/health", headers=_headers(), timeout=3.0)
        return r.status_code < 400
    except Exception:
        return False


def status() -> dict:
    live = connected()
    return {"connected": live, "mode": "live" if live else "demo",
            "api_url": VIBEXGEN_API_URL or None, "niche": NICHE,
            "capabilities": ["trends", "generate"],
            "engine": "vibexgen mcp-api-server" if live else None,
            "note": ("vibexgen connected — trends via live YouTube search, generation via the "
                     "vibexgen video agent" if live else
                     "demo mode — set VIBEXGEN_API_URL + an authenticated VIBEXGEN_API_KEY to go live")}


def _search_results(payload: dict) -> list:
    """ytdlp_search returns MCP TextContent whose .text is a preamble + a JSON array. Pull the array."""
    try:
        text = payload["result"][0]["text"]
        start = text.find("[")
        return json.loads(text[start:]) if start >= 0 else []
    except Exception:
        return []


def trends(topic: str = "") -> dict:
    """Trending angles for the niche/topic. Live: real YouTube search via vibexgen. Demo (not connected): curated samples,
    labelled as such. A LIVE search that fails says so and returns no angles: samples are never passed off as its answer."""
    topic = (topic or NICHE).strip()
    if not _LIVE:
        return {"source": "demo", "status": "sample", "topic": topic, "hashtags": _DEMO_HASHTAGS, "angles": _DEMO_ANGLES,
                "note": "vibexgen is not connected: these are built-in samples, not a search"}
    try:
        r = httpx.post(f"{VIBEXGEN_API_URL}/tools/ytdlp_search", headers=_headers(),
                       json={"query": f"{topic} shorts", "max_results": 6}, timeout=25.0)
    except Exception as e:  # noqa: BLE001
        return _no_trends(topic, f"vibexgen did not answer ({type(e).__name__})")
    if r.status_code >= 400:
        return _no_trends(topic, f"vibexgen answered HTTP {r.status_code}")
    try:
        vids = _search_results(r.json())
        angles = []
        for v in vids[:5]:
            title = (v.get("title") or "").strip()
            if not title:
                continue
            views = v.get("view_count") or 0
            angles.append({
                "format": title,
                "hook": f"{views:,} views · {v.get('uploader', '')}".strip(" ·"),
                "why": "trending on YouTube right now — model a short on this angle",
                "url": v.get("url"),
                # a usable post/clip seed so Draft/Generate don't echo raw stats
                "seed": f"Create a {topic} short inspired by: {title}",
            })
    except Exception as e:  # noqa: BLE001
        return _no_trends(topic, f"vibexgen's answer could not be read ({type(e).__name__})")
    if not angles:
        return _no_trends(topic, "the search returned no video")
    return {"source": "vibexgen", "status": "ok", "topic": topic, "hashtags": _DEMO_HASHTAGS, "angles": angles}


def _no_trends(topic: str, why: str) -> dict:
    return {"source": "vibexgen", "status": "unavailable", "topic": topic, "hashtags": [], "angles": [], "error": why}


#: what ``generate`` can report. Only ``processed`` says vibexgen finished the work; nothing here ever says "queued".
GENERATE_STATES = ("processed", "submitted", "failed", "unknown", "not_connected")


def generate(prompt: str, kind: str = "clip") -> dict:
    """Request a clip via the vibexgen video agent (its /chat agent plans and runs the edit).

    ``processed``: it reported success and finished. ``submitted``: it accepted a background task (``task_id``); that is not
    a finished render. ``failed``: it refused or reported an error. ``unknown``: no answer, so whether it started is not
    known. ``not_connected``: nothing was sent, so nothing is waiting anywhere."""
    prompt = (prompt or "").strip()
    base = {"prompt": prompt, "kind": kind}
    if not _LIVE:
        return {**base, "source": "demo", "status": "not_connected",
                "note": "nothing was queued or rendered: connect vibexgen (VIBEXGEN_API_URL + an authenticated "
                        "VIBEXGEN_API_KEY) to route this through the real video agent"}
    base["source"] = "vibexgen"
    try:
        r = httpx.post(f"{VIBEXGEN_API_URL}/chat", headers=_headers(), json={"message": prompt}, timeout=30.0)
    except httpx.ConnectError as e:
        return {**base, "status": "failed", "error": f"vibexgen could not be reached ({type(e).__name__}): nothing was started"}
    except Exception as e:  # noqa: BLE001  (a timeout or broken connection AFTER the request may have started work)
        return {**base, "status": "unknown", "error": f"no answer from vibexgen ({type(e).__name__}): whether it started this is not known"}
    if r.status_code >= 400:
        return {**base, "status": "failed", "error": f"vibexgen answered HTTP {r.status_code}"}
    try:
        d = r.json()
        success, op, task = d.get("success"), d.get("operation") or {}, d.get("task_id")
        out = {**base, "response": d.get("response"), "operation": op.get("tool"), "task_id": task}
    except Exception as e:  # noqa: BLE001
        return {**base, "status": "unknown", "error": f"vibexgen's answer could not be read ({type(e).__name__})"}
    if success is not True:
        return {**out, "status": "failed", "error": str(d.get("error") or d.get("response") or "vibexgen reported that it did not succeed")[:500]}
    return {**out, "status": "submitted" if task else "processed"}
