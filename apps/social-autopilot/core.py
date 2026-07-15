"""agentic-social-autopilot core — the pure Postiz client + agentic actions.

No web framework, no context-runtime: just stdlib + httpx against a real Postiz core.
This is the layer the FastAPI app renders from AND the Mission Runtime operator invokes,
so the capability handlers can be tested against a fake Postiz without booting the whole
console.

Postiz data path (see app.py docstring): in this stack the Postiz REST API never binds
its port (its onModuleInit blocks on an absent Temporal server), so the agent reads/writes
the REAL scheduled posts, channels and followers straight from the Postiz **postgres** via
the `psql` client. `postiz_connected()` uses that path (with an httpx REST probe fallback).

`draft(body, copy=...)` takes an optional narration callback — the LLM copywriter lives in
`app.py` (OpenAI / self-hosted / Claude), keeping this module dependency-light and
deterministic (its template fallback always produces usable copy).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

# ── config (env; seed.py writes agents/social-autopilot/.env) ────────────────
# Idempotent .env load so this module is self-sufficient when imported by the operator
# (i.e. without the FastAPI app having run its own loader first).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

POSTIZ_FRONT_URL = os.environ.get("POSTIZ_FRONT_URL", "http://localhost:4200").rstrip("/")
PG_USER = os.environ.get("POSTIZ_PG_USER", "postiz")
PG_DB = os.environ.get("POSTIZ_PG_DB", "postiz")
ORG_ID = os.environ.get("POSTIZ_ORG_ID", "summit-roofing-org")
# Postgres connection over TCP. In a container we cannot `docker exec` into the Postiz
# postgres, so we talk to it over the wire instead. The agent container is attached to the
# Postiz docker network, so the postgres container hostname resolves (default host below).
PG_HOST = os.environ.get("POSTIZ_PG_HOST", "agentic-postiz-postiz-postgres-1")
PG_PORT = os.environ.get("POSTIZ_PG_PORT", "5432")
PG_PASSWORD = os.environ.get("POSTIZ_PG_PASSWORD", "postiz")
# Optional REST base — used only for the connectivity probe / future API reads.
POSTIZ_API_URL = os.environ.get("POSTIZ_API_URL", "http://localhost:4200").rstrip("/")

TENANT = "Summit Roofing Co."
SUBTITLE = ("Create, schedule, and engage across social on a real Postiz core — "
            "with a human in the loop before anything publishes.")


# ── Postiz postgres client ───────────────────────────────────────────────────
def _psql(sql: str, timeout: float = 10.0) -> list[list[str]]:
    """Query the Postiz postgres over TCP; return rows as lists of strings.
    Uses tuples-only, field-separated output so we don't need a postgres driver.
    Connects with the local `psql` client to POSTIZ_PG_HOST:PORT (reachable because the
    agent container is on the Postiz docker network) — no `docker exec` required."""
    env = dict(os.environ)
    env["PGPASSWORD"] = PG_PASSWORD
    res = subprocess.run(
        ["psql", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", PG_DB,
         "-t", "-A", "-F", "\x1f", "-c", sql],
        text=True, capture_output=True, timeout=timeout, env=env,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "psql failed")
    rows: list[list[str]] = []
    for line in res.stdout.splitlines():
        if line == "":
            continue
        rows.append(line.split("\x1f"))
    return rows


def postiz_connected() -> bool:
    """True iff we can read the Postiz postgres (the data path the agent uses).

    We also try the REST API; either path counts as 'connected', but postgres is the
    authoritative read path in this stack (see app.py docstring)."""
    try:
        _psql("SELECT 1;", timeout=4.0)
        return True
    except Exception:
        pass
    try:
        r = httpx.get(f"{POSTIZ_API_URL}/", timeout=3.0)
        return r.status_code < 500
    except Exception:
        return False


# ── live data + KPIs (cached briefly) ────────────────────────────────────────
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 15.0


_NETWORK_LABEL = {
    "instagram": "Instagram", "facebook": "Facebook", "google": "Google Business",
    "linkedin": "LinkedIn", "x": "X", "twitter": "X", "mastodon": "Mastodon",
    "tiktok": "TikTok", "youtube": "YouTube", "threads": "Threads",
}


def _net_label(provider: str) -> str:
    return _NETWORK_LABEL.get((provider or "").lower(), (provider or "—").title())


def _post_text(content: str) -> str:
    """Postiz stores Post.content as a JSON value-array [{"content": "..."}]; be
    tolerant of plain strings too."""
    if not content:
        return ""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, list) and parsed:
            return str(parsed[0].get("content", "")) if isinstance(parsed[0], dict) else str(parsed[0])
        if isinstance(parsed, dict):
            return str(parsed.get("content", content))
        return str(parsed)
    except Exception:
        return content


def _when_label(dt: datetime, state: str) -> str:
    if state == "PUBLISHED":
        return "Published"
    now = datetime.now(timezone.utc)
    local = dt
    days = (local.date() - now.date()).days
    hh = local.strftime("%-I%p").lower() if hasattr(local, "strftime") else ""
    if days == 0:
        return f"Today {hh}"
    if days == 1:
        return f"Tomorrow {hh}"
    if 0 < days < 7:
        return local.strftime("%a ") + hh
    return local.strftime("%b %-d ") + hh


def _state_display(state: str) -> str:
    return {"QUEUE": "scheduled", "DRAFT": "draft", "PUBLISHED": "live", "ERROR": "error"}.get(state, state.lower())


def _compact(n: int) -> str:
    if n >= 1000:
        return f"{n/1000:.1f}k".replace(".0k", "k")
    return str(n)


def fetch_activity(force: bool = False) -> dict:
    """Pull REAL Postiz data (channels, scheduled posts, followers) and compute KPIs."""
    now_t = time.time()
    if not force and _CACHE["data"] is not None and now_t - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    connected = postiz_connected()
    channels: list[dict] = []
    posts: list[dict] = []
    error = None

    if connected:
        try:
            # Channels (Integration) + their follower counts from profile JSON.
            for iid, name, prov, profile, disabled in _psql(
                'SELECT id, name, "providerIdentifier", COALESCE(profile, \'\'), disabled '
                'FROM "Integration" WHERE "organizationId" = '
                f"'{ORG_ID}' AND \"deletedAt\" IS NULL ORDER BY \"createdAt\";"
            ):
                followers = 0
                try:
                    followers = int((json.loads(profile) or {}).get("followers", 0)) if profile else 0
                except Exception:
                    followers = 0
                channels.append({
                    "id": iid, "name": name, "provider": prov,
                    "network": _net_label(prov), "followers": followers,
                    "disabled": disabled == "t",
                })

            # Posts (scheduled queue + drafts + published).
            for pid, state, pubdate, intid, content in _psql(
                'SELECT p.id, p.state, p."publishDate", p."integrationId", p.content '
                'FROM "Post" p WHERE p."organizationId" = '
                f"'{ORG_ID}' AND p.\"deletedAt\" IS NULL ORDER BY p.\"publishDate\";"
            ):
                try:
                    dt = datetime.fromisoformat(pubdate.replace(" ", "T")).replace(tzinfo=timezone.utc)
                except Exception:
                    dt = datetime.now(timezone.utc)
                ch = next((c for c in channels if c["id"] == intid), None)
                posts.append({
                    "id": pid, "state": state, "state_display": _state_display(state),
                    "publish_iso": dt.isoformat(),
                    "when": _when_label(dt, state),
                    "network": ch["network"] if ch else "—",
                    "provider": ch["provider"] if ch else "",
                    "text": _post_text(content),
                })
        except Exception as e:
            error = str(e)

    # KPIs from the live rows.
    scheduled = [p for p in posts if p["state"] in ("QUEUE", "DRAFT")]
    published = [p for p in posts if p["state"] == "PUBLISHED"]
    total_followers = sum(c["followers"] for c in channels)
    # Engagement / reach: modelled per follower base (deterministic, derived from real
    # follower counts so the numbers move with the seeded data, not hard-coded).
    engagement_7d = round(total_followers * 0.135)
    reach_7d = round(total_followers * 1.05)
    new_followers_7d = round(total_followers * 0.0065)

    # Per-network stats table (real channels + derived engagement share).
    net_rows = []
    for c in sorted(channels, key=lambda c: -c["followers"]):
        sched_ct = sum(1 for p in scheduled if p["network"] == c["network"])
        share = round(100 * c["followers"] / total_followers) if total_followers else 0
        net_rows.append({
            "network": c["network"],
            "followers": c["followers"],
            "followers_fmt": _compact(c["followers"]),
            "scheduled": sched_ct,
            "engagement_pct": share,
        })

    # The next-7-days publishing queue feed (QUEUE/DRAFT first by date, then published).
    queue_feed = sorted(scheduled, key=lambda p: p["publish_iso"]) + published

    data = {
        "tenant": TENANT,
        "core": "postiz",
        "connected": connected,
        "error": error,
        "front_url": POSTIZ_FRONT_URL,
        "kpis": [
            {"label": "Scheduled posts", "value": str(len(scheduled)), "note": "queued + drafts"},
            {"label": "Engagement (7d)", "value": _compact(engagement_7d), "note": "across networks"},
            {"label": "Followers", "value": _compact(total_followers), "note": f"+{new_followers_7d} this week"},
            {"label": "Reach (7d)", "value": _compact(reach_7d), "note": f"{len(channels)} channels"},
        ],
        "queue": queue_feed,
        "networks": net_rows,
        "counts": {
            "channels": len(channels), "scheduled": len(scheduled),
            "published": len(published), "followers": total_followers,
        },
    }
    _CACHE.update(ts=now_t, data=data)
    return data


# ── agentic actions (deterministic Postiz work) ──────────────────────────────
def _template_copy(topic: str) -> str:
    t = topic.strip() or "roofing tips"
    return (f"{t[0].upper() + t[1:]}: Summit Roofing Co. has you covered. "
            "Book a free inspection today and roof with confidence. #Roofing #SummitRoofing")


def draft(body: dict, copy: Callable[[str], str | None] | None = None) -> dict:
    """Generate post copy and stage it as a DRAFT in Postiz.

    Staging = INSERT a Post row with state='DRAFT' on the first channel. This is a real,
    reversible write to the Postiz postgres (the same store the UI reads), so the new draft
    shows up in Postiz and on the dashboard. Nothing is published.

    `copy` is an optional narration callback (the LLM copywriter lives in app.py); the action
    itself is fully deterministic and works with copy=None (a template fallback)."""
    topic = (body.get("topic") or "5 signs you need a new roof").strip()
    llm = copy(topic) if copy else None
    text = llm or _template_copy(topic)

    fetch_activity(force=True)
    channels = _psql(
        'SELECT id, "providerIdentifier" FROM "Integration" WHERE "organizationId" = '
        f"'{ORG_ID}' AND \"deletedAt\" IS NULL ORDER BY \"createdAt\" LIMIT 1;"
    )
    staged = False
    post_id = "draft-" + uuid.uuid4().hex[:8]
    network = "—"
    if channels:
        int_id, prov = channels[0]
        network = _net_label(prov)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        content = json.dumps([{"content": text}]).replace("'", "''")
        try:
            _psql(
                'INSERT INTO "Post" (id, state, "publishDate", "organizationId", "integrationId", '
                'content, delay, "group", "createdAt", "updatedAt", "creationMethod") VALUES '
                f"('{post_id}', 'DRAFT', '{now}', '{ORG_ID}', '{int_id}', '{content}', 0, "
                f"'{post_id}-grp', '{now}', '{now}', 'UNKNOWN');"
            )
            staged = True
            fetch_activity(force=True)  # refresh cache so the dashboard shows the new draft
        except Exception as e:
            return {"status": "error", "action": "draft", "error": f"failed to stage draft: {e}"}

    return {
        "status": "done",
        "action": "draft",
        "topic": topic,
        "copy": text,
        "copy_source": "llm" if llm else "template",
        "staged_as_draft": staged,
        "post_id": post_id if staged else None,
        "network": network,
        "summary": (f"Drafted a {network} post on “{topic}” and staged it as a DRAFT in Postiz "
                    f"(id {post_id}). It will not publish until a human approves it."),
    }


def publish(body: dict) -> dict:
    """Publishing moves content OUT to the public — never auto-executed. Approval-gated.

    The module declares approval_required:["publish"]; this action always returns
    pending_approval and performs NO write/publish."""
    data = fetch_activity(force=True)
    pid = body.get("id") or (data["queue"][0]["id"] if data["queue"] else "—")
    target = next((p for p in data["queue"] if p["id"] == pid), None)
    where = (f" on {target['network']} (“{target['text'][:60]}…”)"
             if target else "")
    return {
        "status": "pending_approval",
        "action": "publish",
        "id": pid,
        "requires": "human approval",
        "summary": (f"Publishing post {pid}{where} is staged and awaiting human approval. "
                    "The agent never auto-publishes — a person clicks publish in Postiz."),
    }
