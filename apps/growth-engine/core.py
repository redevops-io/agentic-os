"""agentic-growth-engine core — the pure Umami REST client + agentic actions.

No web framework, no context-runtime: just httpx against a real Umami core. This is the
layer the FastAPI app renders from AND the Mission Runtime operator invokes, so the
capability handlers can be tested against a fake Umami without booting the whole console.

`analyze(blurb=...)` takes an optional narration callback — the LLM blurb lives in
`app.py` (context-runtime), keeping this module dependency-light and deterministic.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import httpx

# ── config (env; seed.py writes agents/growth-engine/.env) ───────────────────
# Idempotent .env load so this module is self-sufficient when imported by the operator
# (i.e. without the FastAPI app having run its own loader first).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

UMAMI_URL = os.environ.get("UMAMI_URL", "http://localhost:3002").rstrip("/")
WEBSITE_ID = os.environ.get("WEBSITE_ID", "")
ADMIN_USER = os.environ.get("UMAMI_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("UMAMI_ADMIN_PASS", "umami")
UMAMI_FRONT_URL = os.environ.get("UMAMI_FRONT_URL", "http://localhost:3002").rstrip("/")

TENANT = "Meridian Wealth Management"
SUBTITLE = ("Know what's working and put spend where it pays — lead-source attribution "
            "on a real Umami core, with a human in the loop before budget moves.")

# Illustrative economics for the growth model. CPL/ROAS are derived from REAL Umami
# traffic + conversions; the per-click spend rates and the lead value are static
# planning assumptions (actual ad spend lives in Google/Meta Ads, not Umami).
LEAD_VALUE = 850.0  # avg first-year advisory revenue per new client, USD (planning assumption)
# Static cost-per-click by paid channel (USD); organic / referral channels cost $0.
CHANNEL_CPC = {"google": 4.10, "linkedin": 2.30}
# Map a Umami utm_source / referrer to a display channel + whether it's paid.
CHANNEL_LABELS = {
    "google": "Google Ads",
    "linkedin": "LinkedIn",
    "seminar-qr": "Seminar QR",
    "(none)": "Organic / Direct",
}


# --- Umami REST client -------------------------------------------------------
_TOKEN: dict = {"value": None, "ts": 0.0}
_TOKEN_TTL = 600.0  # re-login every 10 min


def _token() -> str | None:
    now = time.time()
    if _TOKEN["value"] and now - _TOKEN["ts"] < _TOKEN_TTL:
        return _TOKEN["value"]
    try:
        r = httpx.post(f"{UMAMI_URL}/api/auth/login",
                       json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=8.0)
        r.raise_for_status()
        tok = r.json().get("token")
        _TOKEN.update(value=tok, ts=now)
        return tok
    except Exception:
        return None


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}"}


def umami_connected() -> bool:
    """True iff Umami's heartbeat endpoint returns 200 + ok."""
    try:
        r = httpx.get(f"{UMAMI_URL}/api/heartbeat", timeout=3.0)
        return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        return False


def _range_ms(days: int = 30) -> tuple[int, int]:
    now = time.time()
    start = int((now - days * 86400) * 1000)
    end = int((now + 86400) * 1000)  # +1d so "today" is fully included
    return start, end


def _get_stats(start: int, end: int) -> dict:
    r = httpx.get(f"{UMAMI_URL}/api/websites/{WEBSITE_ID}/stats",
                  headers=_headers(), params={"startAt": start, "endAt": end}, timeout=10.0)
    r.raise_for_status()
    return r.json()


def _get_metric(mtype: str, start: int, end: int) -> list[dict]:
    """GET /metrics?type=... -> list of {x,y}. Returns [] on any error/empty."""
    try:
        r = httpx.get(f"{UMAMI_URL}/api/websites/{WEBSITE_ID}/metrics",
                      headers=_headers(),
                      params={"type": mtype, "startAt": start, "endAt": end}, timeout=10.0)
        r.raise_for_status()
        body = r.json()
        return body if isinstance(body, list) else []
    except Exception:
        return []


# --- live data + KPIs (cached briefly) ---------------------------------------
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 15.0


def _channel_label(source: str) -> str:
    return CHANNEL_LABELS.get(source or "(none)", source or "Organic / Direct")


def fetch_activity(force: bool = False) -> dict:
    """Pull REAL Umami data and compute the growth KPIs + attribution the dashboard renders."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    connected = umami_connected()
    start, end = _range_ms(30)
    stats: dict = {}
    referrers: list[dict] = []
    utm_sources: list[dict] = []
    utm_mediums: list[dict] = []
    utm_campaigns: list[dict] = []
    error = None
    if connected and WEBSITE_ID:
        try:
            stats = _get_stats(start, end)
            utm_sources = _get_metric("utmSource", start, end)
            utm_mediums = _get_metric("utmMedium", start, end)
            utm_campaigns = _get_metric("utmCampaign", start, end)
            referrers = _get_metric("referrer", start, end)
        except Exception as e:
            error = str(e)

    pageviews = int(stats.get("pageviews", 0) or 0)
    visitors = int(stats.get("visitors", 0) or 0)
    visits = int(stats.get("visits", 0) or 0)
    bounces = int(stats.get("bounces", 0) or 0)

    # --- lead-source attribution from REAL utm_source (fall back to referrer) ----
    src_rows = utm_sources if utm_sources else referrers
    attributed = sum(int(s.get("y", 0)) for s in src_rows)
    # Sessions not tagged with a paid/referral source are organic / direct.
    organic = max(visits - attributed, 0)

    channels: list[dict] = []
    for s in src_rows:
        key = (s.get("x") or "").split(".")[0]  # google.com -> google
        channels.append({"key": key, "label": _channel_label(key),
                         "leads": int(s.get("y", 0)), "paid": key in CHANNEL_CPC})
    if organic > 0:
        channels.append({"key": "(none)", "label": "Organic / Direct",
                         "leads": organic, "paid": False})
    channels.sort(key=lambda c: c["leads"], reverse=True)

    total_leads = sum(c["leads"] for c in channels) or 1

    # --- conversions: estimate from REAL traffic. A booked-lead conversion rate is
    # applied to visits (illustrative); paid channels convert a touch better.
    def _conv(c: dict) -> int:
        rate = 0.34 if c["paid"] else (0.28 if c["key"] == "seminar-qr" else 0.22)
        return max(round(c["leads"] * rate), 0)

    # --- per-channel spend / CPL / ROAS (illustrative economics on real volume) --
    table_rows: list[list[str]] = []
    bar_items: list[dict] = []
    total_spend = 0.0
    total_value = 0.0
    for c in channels:
        clicks_per_lead = 6  # planning assumption: ~6 sessions per qualified lead
        cpc = CHANNEL_CPC.get(c["key"], 0.0)
        spend = c["leads"] * clicks_per_lead * cpc
        conv = _conv(c)
        value = conv * LEAD_VALUE
        total_spend += spend
        total_value += value
        cpl = (spend / conv) if conv and spend else 0.0
        roas = (value / spend) if spend else None
        pct = int(round(100 * c["leads"] / total_leads))
        c["pct"] = pct
        c["spend"] = spend
        c["conv"] = conv
        c["cpl"] = cpl
        c["roas"] = roas
        bar_items.append({"label": c["label"], "pct": pct})
        table_rows.append([
            c["label"],
            str(c["leads"]),
            "$0" if spend == 0 else f"${spend:,.0f}",
            "$0" if not cpl else f"${cpl:,.0f}",
            "∞" if roas is None else f"{roas:,.1f}x",
        ])

    total_conv = sum(c["conv"] for c in channels)
    blended_cpl = (total_spend / total_conv) if total_conv and total_spend else 0.0
    blended_roas = (total_value / total_spend) if total_spend else None
    booked_rate = round(100 * total_conv / total_leads) if total_leads else 0
    top_channel = channels[0]["label"] if channels else "—"

    # --- conversion funnel from REAL volume (illustrative downstream rates) ------
    qualified = round(total_leads * 0.70)
    estimates = round(total_leads * 0.48)
    booked = total_conv
    funnel = {
        "title": "Lead-to-client funnel (30d)",
        "items": [
            {"label": "Leads (sessions)", "pct": 100, "value": str(total_leads)},
            {"label": "Qualified", "pct": 70, "value": str(qualified)},
            {"label": "Proposals sent", "pct": 48, "value": str(estimates)},
            {"label": "New clients", "pct": booked_rate, "value": str(booked)},
        ],
    }

    kpis = [
        {"label": "Cost per lead", "value": ("$0" if not blended_cpl else f"${blended_cpl:,.0f}"),
         "note": "blended, paid channels"},
        {"label": "Leads (30d)", "value": str(total_leads), "note": f"{visitors} visitors · {pageviews} pageviews"},
        {"label": "ROAS", "value": ("∞" if blended_roas is None else f"{blended_roas:,.1f}x"),
         "note": "illustrative · blended"},
        {"label": "Booked rate", "value": f"{booked_rate}%", "note": f"top channel: {top_channel}"},
    ]

    data = {
        "tenant": TENANT,
        "core": "umami",
        "connected": connected,
        "error": error,
        "front_url": UMAMI_FRONT_URL,
        "kpis": kpis,
        "bars": {"title": "Leads by source (30d)", "items": bar_items},
        "funnel": funnel,
        "table": {
            "title": "Channel performance · spend / CPL / ROAS",
            "head": ["Channel", "Leads", "Spend", "Cost / lead", "ROAS"],
            "rows": table_rows,
        },
        "channels": channels,
        "totals": {
            "pageviews": pageviews, "visitors": visitors, "visits": visits, "bounces": bounces,
            "leads": total_leads, "conversions": total_conv, "spend": round(total_spend, 2),
            "blended_cpl": round(blended_cpl, 2),
            "blended_roas": (None if blended_roas is None else round(blended_roas, 2)),
            "booked_rate": booked_rate, "top_channel": top_channel,
        },
        "utm": {
            "sources": utm_sources, "mediums": utm_mediums, "campaigns": utm_campaigns,
            "referrers": referrers,
        },
    }
    _CACHE.update(ts=now, data=data)
    return data


# ── agentic actions (deterministic Umami attribution work) ───────────────────
def analyze(blurb: Callable[[str], str | None] | None = None) -> dict:
    """Summarize which channels perform, straight from REAL Umami attribution data.

    Read-only: computes lead-source attribution + a shift recommendation from the live
    data. `blurb` is an optional narration callback (the LLM one-liner lives in app.py);
    the action itself is fully deterministic and works with blurb=None.
    """
    data = fetch_activity(force=True)
    channels = data.get("channels", [])
    findings = []
    for c in channels:
        roas = "∞" if c.get("roas") is None else f"{c['roas']:.1f}x"
        cpl = "$0" if not c.get("cpl") else f"${c['cpl']:,.0f}"
        findings.append({
            "channel": c["label"], "leads": c["leads"], "share_pct": c.get("pct", 0),
            "spend": round(c.get("spend", 0.0), 2), "cost_per_lead": cpl, "roas": roas,
            "paid": c.get("paid", False),
        })

    t = data["totals"]
    rated = [c for c in channels if c.get("roas") is not None]
    best = max(rated, key=lambda c: c["roas"]) if rated else None
    worst = min(rated, key=lambda c: c["roas"]) if rated else None
    rec = None
    if best and worst and best["label"] != worst["label"]:
        rec = (f"Shift budget from {worst['label']} (ROAS {worst['roas']:.1f}x) to "
               f"{best['label']} (ROAS {best['roas']:.1f}x). Use action "
               "'reallocate_budget' — it is approval-gated.")

    summary = (f"Top channel is {t['top_channel']} by lead volume. "
               f"{t['leads']} leads / {t['visitors']} visitors over 30d, "
               f"{t['conversions']} est. conversions ({t['booked_rate']}% booked rate), "
               f"blended CPL ${t['blended_cpl']:,.0f}, blended ROAS "
               f"{'∞' if t['blended_roas'] is None else str(t['blended_roas'])+'x'}.")

    reasoning = blurb(
        "You are a growth marketing agent for a wealth management firm. In ONE sentence, "
        f"advise on this REAL channel data: {findings}. Be concrete. Final answer only."
    ) if blurb else None

    out = {
        "status": "done",
        "action": "analyze",
        "summary": summary,
        "findings": findings,
        "recommendation": rec,
        "source": "real Umami stats + UTM/referrer metrics (30d)",
    }
    if reasoning:
        out["reasoning"] = reasoning
    return out


def reallocate_budget(body: dict) -> dict:
    """Budget changes move ad spend (in external Ads platforms) — NEVER auto-executed.

    Module declares approval_required:[budget_change]; we stage the change and return
    pending_approval so a human signs off in the Ads platform.
    """
    src = body.get("from", "linkedin")
    dst = body.get("to", "google")
    amount = body.get("amount", 600)
    try:
        amt_txt = f"${float(amount):,.0f}"
    except Exception:
        amt_txt = str(amount)
    return {
        "status": "pending_approval",
        "action": "reallocate_budget",
        "approval_required": "budget_change",
        "from": src, "to": dst, "amount": amount,
        "requires": "human approval",
        "summary": (f"Staged budget change: shift {amt_txt} from {src} to {dst}. "
                    "Not executed — ad spend lives in the external Ads platform (Google/Meta), "
                    "and budget moves are approval-gated. Awaiting human approval."),
    }
