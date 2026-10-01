"""edge-sentinel · runtime-DAST plane — proxy to the deploy-scan MCP service (nmap / nuclei / ZAP).

The RUNTIME half of exposure security: scan a LIVE deployment for exploitable weakness (the
complement of the supply-chain/Trivy plane, which inspects code & packages pre-deploy). The
scanners live in the separate deploy-scan service; this module calls them over MCP/HTTP JSON-RPC.

Scans are async (start → poll → results) so each call stays quick; scope is fail-closed on the
deploy-scan side (SCAN_ALLOWLIST), and starting a scan is staged for human approval (side_effecting).

Self-contained: no context-runtime / agentic-os deps. When DEPLOY_SCAN_MCP_URL is unset the plane
reports itself unavailable (every tool degrades gracefully).

Config (env): DEPLOY_SCAN_MCP_URL.
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

DEPLOY_SCAN_MCP_URL = os.getenv("DEPLOY_SCAN_MCP_URL", "")


def _call(tool: str, arguments: dict) -> dict:
    if not DEPLOY_SCAN_MCP_URL:
        return {"error": "runtime scanning is unavailable (DEPLOY_SCAN_MCP_URL unset)"}
    try:
        r = httpx.post(DEPLOY_SCAN_MCP_URL,
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": tool, "arguments": arguments}}, timeout=30.0)
        r.raise_for_status()
        res = r.json().get("result") or {}
        return res.get("structuredContent") or {"error": "malformed deploy-scan response"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"deploy-scan unreachable: {e}"}


def scan(a: dict) -> dict:
    target = ((a or {}).get("target") or "").strip()
    profile = (a or {}).get("profile") or "web-baseline"
    if not target:
        return {"text": "Give me a target (host or URL) to scan — it must be in the deploy-scan allowlist.", "data": {}}
    d = _call("scan_start", {"target": target, "profile": profile})
    if d.get("error"):
        return {"text": f"Couldn't start the scan: {d['error']}", "data": d}
    return {"text": f"Started a {profile} scan of {target} (job {d.get('job_id')}). "
                    f"Ask for scan_status to watch it, then scan_results for the findings.", "data": d}


def status(a: dict) -> dict:
    job = ((a or {}).get("job_id") or "").strip()
    d = _call("scan_status", {"job_id": job})
    if d.get("error"):
        return {"text": d["error"], "data": d}
    return {"text": f"Scan {job}: {d.get('state')} — {len(d.get('tools_done', []))}/{len(d.get('tools', []))} "
                    f"tool(s) done, {d.get('elapsed_s')}s elapsed.", "data": d}


def results(a: dict) -> dict:
    job = ((a or {}).get("job_id") or "").strip()
    d = _call("scan_results", {"job_id": job})
    if d.get("error"):
        return {"text": d["error"], "data": d}
    if d.get("state") != "done":
        return {"text": f"Scan {job} is {d.get('state')} — not ready; poll scan_status.", "data": d}
    s = d.get("summary", {})
    by = ", ".join(f"{k}: {v}" for k, v in (s.get("by_severity") or {}).items()) or "no findings"
    top = d.get("findings", [])[:12]
    lines = [f"[{f['severity']}] {f['name']} @ {f.get('target', '')}" + (f" ({f['ref']})" if f.get("ref") else "")
             for f in top]
    return {"text": f"Scan of {s.get('target')}: {s.get('total')} finding(s) ({by}); "
                    f"max severity {s.get('max_severity')}." + ("\n" + "\n".join(lines) if lines else ""), "data": d}
