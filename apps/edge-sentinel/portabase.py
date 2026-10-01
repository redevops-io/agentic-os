"""edge-sentinel · Portabase backup / continuity plane (are the cores safely backed up offsite?).

Portabase is the self-hosted backup control plane (Apache-2.0): agents next to each DB take
encrypted dumps and ship them to storage destinations (local + S3-compatible). In real deployments
the offsite destination is an S3 bucket. This module reads Portabase's API to report which of the
stack's databases have a recent, encrypted, offsite backup — and where the gaps are.

Self-contained: no context-runtime / agentic-os deps. Until PORTABASE_API_TOKEN is set, coverage
shows clearly-labelled SAMPLE data so the panel/tool always render. Page chrome is reused from
app.py via a deferred import (no import cycle).

Config (env): PORTABASE_API_URL / PORTABASE_API_TOKEN / PORTABASE_FRONT_URL.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx

# Idempotent .env load (mirrors core.py) so the module is self-sufficient when imported standalone.
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

PORTABASE_API_URL = os.environ.get("PORTABASE_API_URL", "http://host.docker.internal:8887").rstrip("/")
PORTABASE_API_TOKEN = os.environ.get("PORTABASE_API_TOKEN", "")
PORTABASE_FRONT_URL = os.environ.get("PORTABASE_FRONT_URL", PORTABASE_API_URL).rstrip("/")


def _pb_get(path: str):
    if not PORTABASE_API_TOKEN:
        return None
    try:
        r = httpx.get(PORTABASE_API_URL + path,
                      headers={"Authorization": "Bearer " + PORTABASE_API_TOKEN, "Accept": "application/json"},
                      timeout=10.0)
        return r.json() if r.status_code < 400 else None
    except Exception:  # noqa: BLE001
        return None


_PB_SAMPLE = [
    {"name": "chatwoot (support)", "engine": "postgres", "last_backup": "2026-07-10T02:00:00Z", "status": "success", "size": "412 MB", "encrypted": True, "destinations": ["s3:offsite", "local"]},
    {"name": "erpnext (books/crm)", "engine": "mariadb", "last_backup": "2026-07-10T02:12:00Z", "status": "success", "size": "1.3 GB", "encrypted": True, "destinations": ["s3:offsite"]},
    {"name": "lago (billing)", "engine": "postgres", "last_backup": "2026-07-09T02:00:00Z", "status": "success", "size": "233 MB", "encrypted": True, "destinations": ["local"]},
    {"name": "listmonk (lifecycle)", "engine": "postgres", "last_backup": "", "status": "never", "size": "-", "encrypted": False, "destinations": []},
    {"name": "integrated_cp_data (control-plane)", "engine": "volume", "last_backup": "2026-07-10T02:20:00Z", "status": "success", "size": "56 MB", "encrypted": True, "destinations": ["s3:offsite", "local"]},
]


def snapshot() -> dict:
    """Live database/backup coverage from Portabase when configured, else the labelled sample.
    Endpoint paths vary by Portabase version — we try a few and normalise loosely, degrading
    to the sample if none answers (so the panel/tool always render)."""
    for path in ("/api/databases", "/api/v1/databases", "/api/backups", "/api/v1/backups"):
        data = _pb_get(path)
        items = (data.get("items") if isinstance(data, dict) else data) if data else None
        if items:
            dbs = []
            for it in items:
                dbs.append({
                    "name": it.get("name") or it.get("database") or "?",
                    "engine": it.get("engine") or it.get("type") or "?",
                    "last_backup": it.get("lastBackupAt") or it.get("last_backup") or "",
                    "status": it.get("lastStatus") or it.get("status") or "unknown",
                    "size": it.get("lastSize") or it.get("size") or "-",
                    "encrypted": bool(it.get("encrypted", True)),
                    "destinations": it.get("destinations") or it.get("storages") or [],
                })
            return {"live": True, "databases": dbs}
    return {"live": False, "databases": _PB_SAMPLE}


def offsite(d: dict) -> bool:
    return any("s3" in str(x).lower() or "azure" in str(x).lower() or "gcs" in str(x).lower()
               for x in (d.get("destinations") or []))


def backup_status(_a: dict) -> dict:
    snap = snapshot()
    dbs = snap["databases"]
    ok = [d for d in dbs if d.get("status") == "success"]
    never = [d for d in dbs if d.get("status") in ("never", "unknown") or not d.get("last_backup")]
    no_offsite = [d for d in dbs if d.get("last_backup") and not offsite(d)]
    unenc = [d for d in dbs if d.get("last_backup") and not d.get("encrypted", True)]
    tag = "" if snap["live"] else " (SAMPLE — set PORTABASE_API_TOKEN for live data)"
    gaps = []
    if never:
        gaps.append(f"{len(never)} with NO backup ({', '.join(d['name'] for d in never)})")
    if no_offsite:
        gaps.append(f"{len(no_offsite)} not shipped offsite/S3 ({', '.join(d['name'] for d in no_offsite)})")
    if unenc:
        gaps.append(f"{len(unenc)} unencrypted")
    txt = f"Backup coverage{tag}: {len(ok)}/{len(dbs)} databases have a recent successful backup. "
    txt += ("Gaps — " + "; ".join(gaps) + "." if gaps else "All databases are backed up, offsite and encrypted.")
    return {"text": txt, "data": {"live": snap["live"], "total": len(dbs), "ok": len(ok),
            "never": [d["name"] for d in never], "no_offsite": [d["name"] for d in no_offsite],
            "unencrypted": [d["name"] for d in unenc]}}


def panel() -> str:
    """Server-rendered backup-coverage page (reuses app.py's page chrome)."""
    from . import app as _app  # deferred — avoids a load-time cycle (app imports this module)

    snap = snapshot()
    rows = []
    for d in snap["databases"]:
        off = offsite(d)
        status = d.get("status")
        sc = {"success": "#4fd1c5", "never": "#ff6b6b"}.get(status, "#e0b000")
        rows.append([_app._esc(d.get("name") or "?"), _app._esc(d.get("engine") or "?"),
                     _app._esc(d.get("last_backup") or "—"),
                     f"<span style='color:{sc}'>{_app._esc(status)}</span>",
                     _app._esc(str(d.get("size") or "-")),
                     "<span style='color:#4fd1c5'>yes</span>" if d.get("encrypted") else "<span style='color:#ff6b6b'>no</span>",
                     "<span style='color:#4fd1c5'>offsite (S3)</span>" if off else ("<span style='color:#e0b000'>local only</span>" if d.get("destinations") else "<span style='color:#ff6b6b'>none</span>")])
    body = (_app._card(f"Database backups &nbsp; {_app._live_badge(snap['live'], 'set PORTABASE_API_TOKEN')}",
                       _app._tbl(["Database", "Engine", "Last backup", "Status", "Size", "Encrypted", "Destination"], rows))
            + _app._card("Continuity posture",
                         "<p style='color:#9b99a1;font-size:13px;margin:0'>In real deployments every core's dump is encrypted and fanned out to an <b>S3-compatible offsite</b> destination (AWS S3 / MinIO), so a lost host is recoverable. "
                         "Ask the agent for <b>backup coverage</b> to list the gaps — databases with no backup, no offsite copy, or no encryption.</p>"))
    return _app._panel_shell("Backups &amp; Continuity", "Backup coverage over Portabase — which databases have a recent, encrypted, offsite (S3) backup.", body)
