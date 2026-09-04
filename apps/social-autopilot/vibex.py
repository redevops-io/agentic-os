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
    """Trending angles for the niche/topic. Live: real YouTube search via vibexgen; else curated."""
    topic = (topic or NICHE).strip()
    if _LIVE:
        try:
            r = httpx.post(f"{VIBEXGEN_API_URL}/tools/ytdlp_search", headers=_headers(),
                           json={"query": f"{topic} shorts", "max_results": 6}, timeout=25.0)
            if r.status_code < 400:
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
                if angles:
                    return {"source": "vibexgen", "topic": topic,
                            "hashtags": _DEMO_HASHTAGS, "angles": angles}
        except Exception:
            pass
    return {"source": "demo", "topic": topic, "hashtags": _DEMO_HASHTAGS, "angles": _DEMO_ANGLES}


def generate(prompt: str, kind: str = "clip") -> dict:
    """Request a clip via the vibexgen video agent. Live: routes the prompt through vibexgen's
    /chat agent (which plans the edit/render); demo: a queued placeholder."""
    prompt = (prompt or "").strip()
    if _LIVE:
        try:
            r = httpx.post(f"{VIBEXGEN_API_URL}/chat", headers=_headers(),
                           json={"message": prompt}, timeout=30.0)
            if r.status_code < 400:
                d = r.json()
                op = d.get("operation") or {}
                return {"source": "vibexgen",
                        "status": "processed" if d.get("success") else "error",
                        "response": d.get("response"),
                        "operation": op.get("tool"),
                        "task_id": d.get("task_id"), "prompt": prompt}
        except Exception:
            pass
    return {"source": "demo", "status": "queued",
            "prompt": prompt, "kind": kind,
            "note": "connect vibexgen (VIBEXGEN_API_URL + an authenticated VIBEXGEN_API_KEY) to "
                    "route this through the real video agent; in the demo, generation is stubbed"}
