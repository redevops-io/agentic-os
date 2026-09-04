"""agentic-market-radar core — the pure changedetection.io REST client + agentic actions.

No web framework, no context-runtime: just httpx against a real changedetection.io core.
This is the layer the FastAPI app renders from AND the Mission Runtime operator invokes, so
the capability handlers can be tested against a fake changedetection without booting the
whole console.

`brief(body, blurb=...)` takes an optional narration callback — the LLM summary lives in
`app.py` (context-runtime), keeping this module dependency-light and deterministic.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import httpx

# ── config (env; seed.py writes agents/market-radar/.env) ────────────────────
# Idempotent .env load so this module is self-sufficient when imported by the operator
# (i.e. without the FastAPI app having run its own loader first).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

CD_API_URL = os.environ.get("CD_API_URL", "http://localhost:5001").rstrip("/")
CD_API_KEY = os.environ.get("CD_API_KEY", "")
CD_FRONT_URL = os.environ.get("CD_FRONT_URL", "http://localhost:5001").rstrip("/")

TENANT = "Meridian Wealth Management"
SUBTITLE = ("Watch every competitor and get briefed before they move — competitive "
            "intelligence on a real changedetection.io core, with the human in the loop.")


# ── changedetection REST client ──────────────────────────────────────────────
def _headers() -> dict:
    return {"x-api-key": CD_API_KEY, "Content-Type": "application/json"}


def cd_systeminfo() -> dict | None:
    """Return changedetection's /api/v1/systeminfo dict, or None if unreachable/unauthed."""
    try:
        r = httpx.get(f"{CD_API_URL}/api/v1/systeminfo", headers=_headers(), timeout=3.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def cd_connected() -> bool:
    """True iff changedetection's systeminfo endpoint authenticates and responds."""
    return cd_systeminfo() is not None


def _get(path: str, params: dict | None = None) -> dict | list:
    with httpx.Client(timeout=10.0) as client:
        r = client.get(f"{CD_API_URL}{path}", headers=_headers(), params=params)
        r.raise_for_status()
        return r.json()


# ── live data + KPIs (cached briefly) ────────────────────────────────────────
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 15.0  # seconds — keep the dashboard snappy without hammering the core

_WEEK = 7 * 24 * 3600
# Tags that mark a watch as a price/pricing monitor (drives the "price moves" KPI).
_PRICE_TAGS = {"pricing", "price"}


def _ago(ts: int | None) -> str:
    """Human 'time ago' from an epoch seconds value (0/None => never)."""
    if not ts:
        return "never"
    delta = max(0, int(time.time()) - int(ts))
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _short_url(url: str) -> str:
    u = url.replace("https://", "").replace("http://", "")
    return u[:48] + "…" if len(u) > 48 else u


def _resolve_tag_titles(tags: dict) -> dict:
    """Map tag-uuid -> title from /api/v1/tags."""
    return {uuid: (t.get("title") or uuid) for uuid, t in tags.items()}


def fetch_activity(force: bool = False) -> dict:
    """Pull REAL changedetection watch data and compute the market-radar KPIs."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    info = cd_systeminfo()
    connected = info is not None
    watches: dict = {}
    tag_titles: dict = {}
    error = None
    if connected and CD_API_KEY:
        try:
            watches = _get("/api/v1/watch")  # {uuid: {...}}
            try:
                tag_titles = _resolve_tag_titles(_get("/api/v1/tags"))
            except Exception:
                tag_titles = {}
        except Exception as e:  # network / auth hiccup — surface, don't crash the page
            error = str(e)

    rows: list[dict] = []
    for uuid, w in (watches.items() if isinstance(watches, dict) else []):
        tags = [tag_titles.get(t, t) for t in (w.get("tags") or [])]
        title = w.get("title") or w.get("page_title") or _short_url(w.get("url", ""))
        last_checked = w.get("last_checked") or 0
        last_changed = w.get("last_changed") or 0
        viewed = bool(w.get("viewed", False))
        # "unread" = a real change happened that hasn't been viewed in the UI.
        unread = bool(last_changed) and not viewed
        is_price = bool(_PRICE_TAGS & {t.lower() for t in tags})
        if w.get("last_error"):
            state = "error"
        elif unread:
            state = "changed"
        elif last_checked:
            state = "stable"
        else:
            state = "pending"
        rows.append({
            "uuid": uuid,
            "title": title,
            "url": w.get("url", ""),
            "short_url": _short_url(w.get("url", "")),
            "tags": tags,
            "tag_str": ", ".join(tags) if tags else "—",
            "last_checked": last_checked,
            "last_checked_h": _ago(last_checked),
            "last_changed": last_changed,
            "last_changed_h": _ago(last_changed) if last_changed else "no change yet",
            "viewed": viewed,
            "unread": unread,
            "is_price": is_price,
            "state": state,
        })

    rows.sort(key=lambda r: (r["unread"], r["last_changed"], r["last_checked"]), reverse=True)

    # KPIs straight from the live watches.
    competitors = len(rows)
    changes_week = sum(1 for r in rows if r["last_changed"] and now - r["last_changed"] < _WEEK)
    price_moves = sum(1 for r in rows if r["is_price"] and r["last_changed"]
                      and now - r["last_changed"] < _WEEK)
    unread = sum(1 for r in rows if r["unread"])

    data = {
        "tenant": TENANT,
        "core": "changedetection",
        "connected": connected,
        "version": (info or {}).get("version"),
        "queue_size": (info or {}).get("queue_size"),
        "error": error,
        "front_url": CD_FRONT_URL,
        "kpis": [
            {"label": "Competitors tracked", "value": str(competitors), "note": "watches in core"},
            {"label": "Changes this week", "value": str(changes_week), "note": "across all watches"},
            {"label": "Price moves", "value": str(price_moves), "note": "on pricing pages (7d)"},
            {"label": "Unread changes", "value": str(unread), "note": "need a human look"},
        ],
        "watches": rows,
        "counts": {"watches": competitors, "changes_week": changes_week,
                   "price_moves": price_moves, "unread": unread},
    }
    _CACHE.update(ts=now, data=data)
    return data


# ── agentic actions (deterministic changedetection API work) ─────────────────
def add_watch(body: dict) -> dict:
    """Create a new monitor: POST /api/v1/watch. Adding a watch is non-destructive
    (it only starts observing a public page), so no human approval is required.

    Side-effecting (it creates a watch on the real core); idempotency at the wire is
    provided by the Operator's idempotency-key dedupe.
    """
    url = (body.get("url") or "").strip()
    title = (body.get("title") or "").strip()
    tag = (body.get("tag") or "competitor").strip()
    if not url:
        return {"status": "error", "action": "add_watch",
                "error": "missing 'url'", "requires": "none"}
    payload = {"url": url, "title": title or url, "tag": tag}
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(f"{CD_API_URL}/api/v1/watch", headers=_headers(), json=payload)
        if resp.status_code in (200, 201):
            uuid = (resp.json() or {}).get("uuid")
            fetch_activity(force=True)  # refresh the dashboard cache
            return {
                "status": "done",
                "action": "add_watch",
                "requires": "none (non-destructive — only starts monitoring a public page)",
                "watch": {"uuid": uuid, "url": url, "title": title or url, "tag": tag},
                "summary": f"Now monitoring '{title or url}' ({url}) in changedetection.",
            }
        return {"status": "error", "action": "add_watch",
                "core_status": resp.status_code, "core_body": resp.text[:300]}
    except Exception as e:
        return {"status": "error", "action": "add_watch", "error": str(e)}


def _watch_history(uuid: str) -> list:
    """Return the change-history timestamps for a watch (empty if none yet)."""
    try:
        hist = _get(f"/api/v1/watch/{uuid}/history")
        if isinstance(hist, dict):
            return sorted(hist.keys())
        return list(hist or [])
    except Exception:
        return []


def brief(body: dict, blurb: Callable[[str], str | None] | None = None) -> dict:
    """Summarize recent changes across all watches. Read-only — no approval needed.

    Reads the watch list + each watch's history (GET /api/v1/watch/{uuid}/history);
    `blurb` is an optional narration callback (the LLM one-paragraph brief lives in
    app.py); the action itself is fully deterministic and works with blurb=None.
    """
    data = fetch_activity(force=True)
    items = []
    for w in data["watches"]:
        history = _watch_history(w["uuid"]) if (w["unread"] or w["last_changed"]) else []
        items.append({
            "title": w["title"],
            "url": w["url"],
            "tags": w["tags"],
            "state": w["state"],
            "last_checked": w["last_checked_h"],
            "last_change": w["last_changed_h"],
            "unread": w["unread"],
            "history_points": len(history),
        })

    changed = [i for i in items if i["state"] == "changed"]
    if changed:
        headline = (f"{len(changed)} competitor page(s) changed and are unread: "
                    + "; ".join(f"{i['title']} ({i['last_change']})" for i in changed))
    else:
        headline = (f"No unread changes across {len(items)} tracked competitor page(s); "
                    "all watches are stable or awaiting their first diff.")

    out = {
        "status": "done",
        "action": "brief",
        "requires": "none (read-only intelligence summary)",
        "watches_reviewed": len(items),
        "changed": len(changed),
        "items": items,
        "summary": headline,
    }

    detail = "; ".join(
        f"{i['title']} [{', '.join(i['tags']) or 'untagged'}] — state={i['state']}, "
        f"last change {i['last_change']}" for i in items
    ) or "no watches"
    reasoning = blurb(
        "You are a competitive-intelligence analyst for a wealth management firm (Meridian "
        "Wealth Management). In ONE short paragraph, brief the principal on the current state of "
        f"these competitor/fee/regulatory web monitors: {detail}. Be concrete; if nothing "
        "has changed yet, say what is being watched and why it matters. Final answer only."
    ) if blurb else None
    if reasoning:
        out["reasoning"] = reasoning
    return out
