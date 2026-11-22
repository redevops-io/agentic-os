"""edge-sentinel · static-engine plane — proxy to the static-scan MCP service (Trivy / SkillSpector).

The SUPPLY-CHAIN half of exposure security and the complement of `deploy_scan.py`: that module scans a
LIVE deployment with probe traffic, this one reads a tree of code and skills before anything runs. The
engines live in their own image (`agents/static-scan` in the stack); this module only calls them over
MCP/HTTP JSON-RPC, for the same reason the DAST proxy exists — the code that parses hostile scanner
output does not belong in the process that holds the CrowdSec bouncer key.

Scans are async (start → poll → results): a rootfs or corpus scan takes minutes and an MCP call has to
stay inside the client's ~55 s ceiling.

**What this module refuses to do is the point.** Every answer carries the plane's own coverage verdict
forward instead of summarising it away:

  * `read_as` / `coverage` / `gap_reasons` are printed on the same line as the finding count, so a run
    that scanned nothing cannot be relayed as "no findings";
  * `engine_status` is offered before any count is — an engine that is absent or off its pin makes every
    number it produced unauditable, and the plane already says so in `reads_as`;
  * nothing here recommends, dismisses or remediates. Findings arrive SUPPORTED (the scanner said so,
    here is its evidence); confirming one is a human act in this codebase.

Self-contained: no context-runtime / agentic-os deps. With `STATIC_SCAN_MCP_URL` unset the plane reports
itself unavailable and every tool degrades to that sentence — the plane is never silently half-there.

Config (env): STATIC_SCAN_MCP_URL, STATIC_SCAN_TOKEN (optional bearer for starting scans).
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

STATIC_SCAN_MCP_URL = os.getenv("STATIC_SCAN_MCP_URL", "")
STATIC_SCAN_TOKEN = os.getenv("STATIC_SCAN_TOKEN", "")


def configured() -> bool:
    return bool(STATIC_SCAN_MCP_URL)


def _call(tool: str, arguments: dict) -> dict:
    if not STATIC_SCAN_MCP_URL:
        return {"error": "static scanning is unavailable (STATIC_SCAN_MCP_URL unset)"}
    headers = {"Authorization": f"Bearer {STATIC_SCAN_TOKEN}"} if STATIC_SCAN_TOKEN else {}
    try:
        r = httpx.post(STATIC_SCAN_MCP_URL,
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": tool, "arguments": arguments}},
                       headers=headers, timeout=30.0)
        if r.status_code in (401, 403):
            # Authorisation refused is not "nothing was found", and it must never reach the caller as an
            # empty result: the whole point of this plane is that absence is reported, not inferred.
            return {"error": f"static-scan refused the call ({r.status_code}) — the caller is not "
                             f"authorised to start scans (STATIC_SCAN_TOKEN)"}
        r.raise_for_status()
        res = r.json().get("result") or {}
        if res.get("isError"):
            return {"error": (res.get("content") or [{}])[0].get("text", "static-scan refused the call")}
        return res.get("structuredContent") or {"error": "malformed static-scan response"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"static-scan unreachable: {e}"}


def engines(a: dict) -> dict:
    """Which engines are actually there — read this before believing any finding count."""
    d = _call("engine_status", {})
    if d.get("error"):
        return {"text": f"Couldn't read the static engines: {d['error']}", "data": d}
    lines = []
    for name, av in (d.get("engines") or {}).items():
        mark = "installed" if av.get("installed") else "ABSENT"
        lines.append(f"- {name}: {mark} {av.get('version') or ''} "
                     f"(pin {av.get('pin') or '?'}) — {av.get('reads_as')}")
    state = d.get("state") or {}
    scope = "allow-listed" if d.get("scope_configured") else "NOT allow-listed (STATIC_SCAN_ROOTS empty — nothing can scan)"
    return {"text": (f"Static engines ({scope}; durable case store: "
                     f"{'yes' if state.get('persistent') else 'NO — runs are lost on restart'}, "
                     f"{state.get('cases', 0)} case(s) filed):\n" + "\n".join(lines)
                     + f"\nCoverage board: {d.get('runs_ingested', 0)} recorded run(s)."),
            "data": d}


def board(a: dict) -> dict:
    """What has actually been covered — one row per recorded run, never rolled up into 'clean'."""
    d = _call("engine_status", {})
    if d.get("error"):
        return {"text": f"Couldn't read the coverage board: {d['error']}", "data": d}
    rows = d.get("coverage_board") or []
    lines = [f"- {r.get('engine')} {r.get('target')}: {r.get('coverage')}"
             + (f" ({'; '.join(r.get('gap_reasons') or [])[:120]})" if r.get("coverage") != "complete" else "")
             + f" — {r.get('findings')} finding(s), case {r.get('case_id')}" for r in rows]
    return {"text": (f"{len(rows)} recorded run(s), each with its own verdict — these are never aggregated "
                     f"into 'the estate is clean':\n" + ("\n".join(lines) or "(nothing has scanned yet)")),
            "data": {"coverage_board": rows, "state": d.get("state")}}


def scan(a: dict) -> dict:
    target = ((a or {}).get("target") or "").strip()
    engine = (a or {}).get("engine") or "trivy"
    mode = (a or {}).get("mode") or "fs"
    if not target:
        return {"text": "Give me a path to scan — it must be inside the static-scan allow-list "
                        "(STATIC_SCAN_ROOTS). Engines: trivy | semgrep | skillspector.", "data": {}}
    d = _call("scan_start", {"target": target, "engine": engine, "mode": mode})
    if d.get("error"):
        return {"text": f"Couldn't start the scan: {d['error']}", "data": d}
    return {"text": f"Started a {engine} ({mode}) scan of {target} — job {d.get('job_id')}. "
                    f"Watch it with static_scan_status, then read static_scan_results; a finished job "
                    f"has already written its evidence and its case.",
            "data": d}


def status(a: dict) -> dict:
    job = ((a or {}).get("job_id") or "").strip()
    d = _call("scan_status", {"job_id": job})
    if d.get("error"):
        return {"text": d["error"], "data": d}
    gaps = "; ".join((d.get("gap_reasons") or [])[:3])
    return {"text": f"Scan {job}: {d.get('state')} — coverage {d.get('coverage')}"
                    + (f" (gap: {gaps})" if gaps else "")
                    + f", {d.get('n_findings')} finding(s), case {d.get('case_id') or '(none)'}, "
                    + f"{d.get('elapsed_s')}s elapsed.", "data": d}


def results(a: dict) -> dict:
    job = ((a or {}).get("job_id") or "").strip()
    d = _call("scan_results", {"job_id": job})
    if d.get("error"):
        return {"text": d["error"], "data": d}
    if d.get("state") != "done":
        return {"text": f"Scan {job} is {d.get('state')} — not ready; poll static_scan_status.", "data": d}
    counts = (d.get("summary") or {}).get("by_severity") or {}
    lines = [f"[{(f.get('severity') or '?').upper()}] {f.get('rule_id') or f.get('name')} "
             f"@ {f.get('path') or d.get('target')}" for f in (d.get("findings") or [])[:12]]
    # `read_as` is the plane's own sentence about what this run entitles anyone to say. It is printed
    # first and last on purpose: a finding count above a coverage gap is how a scan becomes a lie.
    return {"text": (f"{d.get('read_as')}\n"
                     f"Case {d.get('case_id') or '(not written)'} — "
                     f"{d.get('findings_total', 0)} finding(s) "
                     + (f"({', '.join(f'{k}: {v}' for k, v in counts.items())}); " if counts else "; ")
                     + f"coverage {d.get('coverage')}."
                     + ("\n" + "\n".join(lines) if lines else "")
                     + ("\n" + d.get("read_as") if lines else "")),
            "data": d}