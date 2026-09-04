"""edge-sentinel core — the pure CrowdSec client + agentic actions.

No web framework, no context-runtime: just stdlib (subprocess for `cscli`) + httpx against
a real CrowdSec core. This is the layer the FastAPI app renders from AND the Mission Runtime
operator invokes, so the capability handlers can be tested against a fake CrowdSec LAPI
without booting the whole SOC console.

Two read paths (mapped from billing → edge-sentinel):
  * decisions: GET /v1/decisions on the LAPI using the bouncer X-Api-Key (httpx).
  * alerts:    `cscli alerts list -o json` shelled out via docker exec (LAPI alert auth
               needs a machine login; cscli is the simplest reliable path).

Write path (the SENSITIVE action): `block_ip` POSTs a ban decision to the LAPI; its
compensating action (saga undo) is `unblock_ip` (DELETE the decision). `triage(blurb=...)`
takes an optional narration callback — the LLM blurb lives in `app.py` (context-runtime),
keeping this module dependency-light and deterministic.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

import httpx

# ── config (env; seed.py writes agents/edge-sentinel/.env) ───────────────────
# Idempotent .env load so this module is self-sufficient when imported by the operator
# (i.e. without the FastAPI app having run its own loader first).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

CROWDSEC_LAPI_URL = os.environ.get("CROWDSEC_LAPI_URL", "http://localhost:8086").rstrip("/")
CROWDSEC_BOUNCER_KEY = os.environ.get("CROWDSEC_BOUNCER_KEY", "")
CROWDSEC_CONTAINER = os.environ.get("CROWDSEC_CONTAINER", "agentic-cores-crowdsec-1")
CROWDSEC_FRONT_URL = os.environ.get("CROWDSEC_FRONT_URL", "http://localhost:8086").rstrip("/")
# How to invoke docker for `cscli` (alerts). On the host this needs `sudo docker`; inside the
# integrated container we run as root with the docker socket mounted, so DOCKER_CMD="docker"
# (no sudo). Override via env.
DOCKER_CMD = os.environ.get("DOCKER_CMD", "sudo docker").split()

TENANT = "Meridian Wealth Management"
SUBTITLE = "Network security & systems, triaged and explained by an agent — on a real CrowdSec core, with a human in the loop before any block."


# ── CrowdSec clients ─────────────────────────────────────────────────────────
def _headers() -> dict:
    return {"X-Api-Key": CROWDSEC_BOUNCER_KEY, "Content-Type": "application/json"}


def _cscli(args: list[str]) -> tuple[int, str, str]:
    """Run `cscli <args>` inside the CrowdSec container. `sudo` for the docker socket."""
    cmd = [*DOCKER_CMD, "exec", CROWDSEC_CONTAINER, "cscli", *args]
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=20)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def crowdsec_connected() -> bool:
    """True iff the LAPI answers (any HTTP status means the service is up)."""
    try:
        r = httpx.get(f"{CROWDSEC_LAPI_URL}/health", timeout=3.0)
        # CrowdSec LAPI has no public /health; a TCP+HTTP response (even 404) is "up".
        return r.status_code < 500
    except Exception:
        try:
            r = httpx.get(f"{CROWDSEC_LAPI_URL}/", timeout=3.0)
            return r.status_code < 500
        except Exception:
            return False


def fetch_decisions() -> list[dict]:
    """Live active decisions via the LAPI bouncer endpoint (GET /v1/decisions)."""
    if not CROWDSEC_BOUNCER_KEY:
        return []
    try:
        r = httpx.get(
            f"{CROWDSEC_LAPI_URL}/v1/decisions",
            headers={"X-Api-Key": CROWDSEC_BOUNCER_KEY},
            timeout=8.0,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception:
        return []


def fetch_alerts() -> list[dict]:
    """Recent alerts via `cscli alerts list -o json` (docker exec)."""
    rc, out, _err = _cscli(["alerts", "list", "-o", "json"])
    if rc != 0 or not out.strip():
        return []
    try:
        return json.loads(out) or []
    except Exception:
        return []


# ── pure display helpers ─────────────────────────────────────────────────────
def _severity(scenario: str) -> str:
    """Map a scenario / reason string to a SOC severity bucket."""
    s = (scenario or "").lower()
    if any(k in s for k in ("bruteforce", "brute", "-bf", "credential", "exploit", "rce", "malware", "c2")):
        return "critical"
    if any(k in s for k in ("probing", "probe", "traversal", "injection", "http-", "web")):
        return "high"
    if any(k in s for k in ("scan", "nmap", "port")):
        return "medium"
    return "low"


def _short_scenario(scenario: str) -> str:
    """Pull the crowdsecurity/<name> token when present, else the raw text."""
    if not scenario:
        return "—"
    for tok in scenario.replace("(", " ").replace(")", " ").split():
        if "/" in tok:
            return tok
    return scenario


def _ago(iso: str) -> str:
    if not iso:
        return "—"
    try:
        from datetime import datetime, timezone

        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        secs = (datetime.now(timezone.utc) - t).total_seconds()
        if secs < 60:
            return f"{int(secs)}s ago"
        if secs < 3600:
            return f"{int(secs // 60)}m ago"
        if secs < 86400:
            return f"{int(secs // 3600)}h ago"
        return f"{int(secs // 86400)}d ago"
    except Exception:
        return iso[:16].replace("T", " ")


# ── live data + KPIs (cached briefly) ────────────────────────────────────────
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 10.0  # seconds — keep the dashboard snappy without hammering CrowdSec


def fetch_activity(force: bool = False) -> dict:
    """Pull REAL CrowdSec data and compute the SOC KPIs the dashboard renders."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    connected = crowdsec_connected()
    decisions = fetch_decisions() if connected else []
    alerts = fetch_alerts() if connected else []

    # --- decisions table (active bans) ---
    decision_rows = []
    for d in decisions:
        scenario = d.get("scenario", "")
        decision_rows.append({
            "value": d.get("value", "—"),
            "scope": d.get("scope", "Ip"),
            "scenario": _short_scenario(scenario),
            "scenario_full": scenario,
            "type": (d.get("type") or "ban").upper(),
            "duration": (d.get("duration") or "").split(".")[0],
            "origin": d.get("origin", "—"),
            "severity": _severity(scenario),
        })
    decision_rows.sort(key=lambda r: {"critical": 0, "high": 1, "medium": 2, "low": 3}[r["severity"]])

    # --- alert feed (newest first) ---
    alert_rows = []
    for a in alerts:
        scenario = a.get("scenario", "")
        src = a.get("source") or {}
        alert_rows.append({
            "scenario": _short_scenario(scenario),
            "scenario_full": scenario,
            "source": src.get("value") or src.get("ip") or "—",
            "scope": src.get("scope", "Ip"),
            "events": a.get("events_count", 0),
            "created_at": a.get("created_at", ""),
            "ago": _ago(a.get("created_at", "")),
            "severity": _severity(scenario),
        })
    alert_rows.sort(key=lambda r: r["created_at"], reverse=True)

    # --- KPIs straight from the live data ---
    active_bans = len([d for d in decisions if (d.get("type") or "ban") == "ban"])

    from datetime import datetime, timezone, timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    def _within_24h(iso: str) -> bool:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")) >= cutoff
        except Exception:
            return True  # if unparseable, count it rather than hide it

    alerts_24h = len([a for a in alerts if _within_24h(a.get("created_at", ""))])

    # top scenarios by count across alerts
    scenario_counts: dict[str, int] = {}
    for a in alerts:
        key = _short_scenario(a.get("scenario", ""))
        scenario_counts[key] = scenario_counts.get(key, 0) + 1
    top_scenarios = sorted(scenario_counts.items(), key=lambda kv: kv[1], reverse=True)

    unique_sources = len({(d.get("value") or "") for d in decisions if d.get("value")})

    last_event = alert_rows[0]["ago"] if alert_rows else "—"

    # threat banner state: any active blocking decision = active threat handled
    has_threat = active_bans > 0

    # scenario bars (% of total alerts) for the SOC "blocked by category" meter
    total_alerts = max(sum(scenario_counts.values()), 1)
    bar_items = [
        {"label": k, "pct": int(round(100 * v / total_alerts)), "count": v}
        for k, v in top_scenarios[:5]
    ]

    data = {
        "tenant": TENANT,
        "core": "crowdsec",
        "connected": connected,
        "front_url": CROWDSEC_FRONT_URL,
        "has_threat": has_threat,
        "kpis": [
            {"label": "Threats blocked", "value": str(active_bans), "note": "active decisions enforced"},
            {"label": "Alerts (24h)", "value": str(alerts_24h), "note": f"{len(alerts)} total in store"},
            {"label": "Source IPs", "value": str(unique_sources), "note": "unique blocked sources"},
            {"label": "Last event", "value": last_event, "note": top_scenarios[0][0] if top_scenarios else "—"},
        ],
        "decisions": decision_rows,
        "alerts": alert_rows,
        "top_scenarios": [{"scenario": k, "count": v} for k, v in top_scenarios],
        "bars": bar_items,
        "counts": {"decisions": len(decisions), "alerts": len(alerts), "sources": unique_sources},
    }
    _CACHE.update(ts=now, data=data)
    return data


# ── agentic actions (deterministic CrowdSec core work) ───────────────────────
def block_ip(body: dict) -> dict:
    """Add a ban decision at the CrowdSec edge — the SENSITIVE, side-effecting write.

    POST /v1/decisions on the LAPI enforces the ban. Gated in the operator
    (approval_required=True), so the Mission Runtime parks it as a HumanTask before
    execution; its compensating action (saga undo) is `unblock_ip`.
    """
    ip = (body.get("ip") or "").strip()
    if not ip:
        return {"status": "error", "action": "block_ip", "error": "missing 'ip'"}
    duration = body.get("duration", "4h")
    reason = body.get("reason", "blocked via edge-sentinel agent (human-approved)")
    decision = {
        "type": "ban", "scope": "Ip", "value": ip,
        "duration": duration, "origin": "cscli", "scenario": reason,
    }
    status_code = None
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            f"{CROWDSEC_LAPI_URL}/v1/decisions",
            headers=_headers(),
            json=[decision],
        )
        status_code = resp.status_code
    ok = status_code in (200, 201, 202)
    fetch_activity(force=True)  # refresh the cache so the dashboard shows it immediately
    return {
        "status": "done" if ok else "error",
        "action": "block_ip",
        "ip": ip,
        "duration": duration,
        "enforced": ok,
        "lapi_status": status_code,
        "summary": (
            f"Banned {ip} for {duration} via CrowdSec (POST /v1/decisions)." if ok
            else f"Failed to ban {ip} (LAPI status {status_code})."
        ),
    }


def unblock_ip(body: dict) -> dict:
    """Compensating action (saga undo) for block_ip — lift the ban.

    DELETE /v1/decisions?ip=<ip> removes the active decision on the LAPI.
    """
    ip = (body.get("ip") or "").strip()
    if not ip:
        return {"status": "error", "action": "unblock_ip", "error": "missing 'ip'"}
    status_code = None
    with httpx.Client(timeout=10.0) as client:
        resp = client.delete(
            f"{CROWDSEC_LAPI_URL}/v1/decisions",
            headers=_headers(),
            params={"ip": ip},
        )
        status_code = resp.status_code
    ok = status_code in (200, 204)
    fetch_activity(force=True)
    return {
        "status": "done" if ok else "error",
        "action": "unblock_ip",
        "ip": ip,
        "lifted": ok,
        "lapi_status": status_code,
        "summary": (
            f"Unbanned {ip} via CrowdSec (DELETE /v1/decisions)." if ok
            else f"Failed to unban {ip} (LAPI status {status_code})."
        ),
    }


def triage(blurb: Callable[[str], str | None] | None = None) -> dict:
    """Summarize the current alerts/decisions (deterministic; LLM blurb optional).

    Read-only: reads live alerts + decisions and explains the posture. `blurb` is an
    optional narration callback (the LLM one-liner lives in app.py); the action itself is
    fully deterministic and works with blurb=None.
    """
    data = fetch_activity(force=True)
    decisions = data["decisions"]
    alerts = data["alerts"]
    by_sev: dict[str, int] = {}
    for a in alerts:
        by_sev[a["severity"]] = by_sev.get(a["severity"], 0) + 1
    top = data["top_scenarios"][:3]

    detail = "; ".join(f"{d['value']} ({d['scenario']}, {d['severity']})" for d in decisions[:6]) or "none"
    summary = (
        f"{len(decisions)} active block(s), {len(alerts)} alert(s) in store. "
        f"Severity mix: " + ", ".join(f"{k}={v}" for k, v in sorted(by_sev.items())) + ". "
        f"Top scenarios: " + ", ".join(f"{s['scenario']}({s['count']})" for s in top) + "."
    )
    out = {
        "status": "done",
        "action": "triage",
        "active_decisions": len(decisions),
        "alerts": len(alerts),
        "severity_breakdown": by_sev,
        "top_scenarios": top,
        "summary": summary,
    }
    reasoning = blurb(
        "You are a SOC analyst agent for a wealth-management firm's network. In ONE sentence, "
        f"triage these active CrowdSec block decisions and recommend a next step: {detail}. "
        "Be concrete and professional. Final answer only, no preamble."
    ) if blurb else None
    if reasoning:
        out["reasoning"] = reasoning
    return out
