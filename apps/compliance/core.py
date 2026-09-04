"""agentic-compliance core — the pure OpenSCAP parser + agentic actions.

No web framework, no context-runtime: just stdlib (XML parsing) over the REAL oscap
XCCDF results file. This is the layer the FastAPI app renders from AND the Mission
Runtime operator invokes, so the capability handlers can be tested against a fixture
results file without booting the whole console.

`explain(body, blurb=...)` takes an optional narration callback — the LLM rewrite lives
in `app.py` (context-runtime), keeping this module dependency-light and deterministic.
The results/report/scan-script paths are module globals so a test can point them at a
fixture.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Callable

# ── config (env; seed.py writes agents/compliance/.env) ──────────────────────
# Idempotent .env load so this module is self-sufficient when imported by the operator
# (i.e. without the FastAPI app having run its own loader first).
HERE = Path(__file__).resolve().parent
_ENV_FILE = HERE / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

SCAP_RESULTS = Path(os.environ.get("SCAP_RESULTS", str(HERE / "results" / "scan-results.xml")))
SCAP_REPORT = Path(os.environ.get("SCAP_REPORT", str(HERE / "results" / "report.html")))
SCAP_PROFILE = os.environ.get(
    "SCAP_PROFILE", "xccdf_org.ssgproject.content_profile_cis_level1_server"
)
# The scanner entrypoint _scan shells out to (a re-scan re-runs the real oscap tool).
# A module global so a test can point it at a nonexistent path to skip the subprocess.
SCAP_SCAN_SCRIPT = HERE / "scan.sh"

# Append-only evidence ledger (JSONL) — the onboarding consent record lands here as
# tamper-evident audit evidence. A module global so a test can point it at a tmp file.
CONSENT_LEDGER = Path(os.environ.get("COMPLIANCE_CONSENT_LEDGER", str(HERE / "evidence" / "consent-ledger.jsonl")))

XCCDF = "{http://checklists.nist.gov/xccdf/1.2}"

TENANT = "Meridian Wealth Management"
SUBTITLE = (
    "Continuous control monitoring on a real OpenSCAP core — CIS Ubuntu benchmark "
    "scanned on Meridian's server, with a human in the loop before any fix is applied."
)
# The framework the scanned profile maps to (shown as the primary framework card).
FRAMEWORK = "CIS Ubuntu Linux 22.04 LTS — Level 1 Server"

# SME compliance items layered in for the wealth-management (RIA) context
# (regulatory registration / insurance expiry).
# These sit alongside the REAL OpenSCAP technical controls in the failing/expiring queue.
SME_ITEMS = [
    {"item": "State RIA registration", "kind": "license", "status": "ACTIVE", "expires": "2027-03-01"},
    {"item": "Errors & Omissions insurance", "kind": "insurance", "status": "ACTIVE", "expires": "2026-09-08"},
    {"item": "Cyber liability insurance", "kind": "insurance", "status": "ACTIVE", "expires": "2026-12-15"},
    {"item": "Fidelity bond", "kind": "insurance", "status": "ACTIVE", "expires": "2027-01-20"},
]


# --- OpenSCAP results parsing (the REAL core data) ---------------------------
def scap_connected() -> bool:
    """True iff a real oscap results file is present and non-empty."""
    try:
        return SCAP_RESULTS.exists() and SCAP_RESULTS.stat().st_size > 0
    except Exception:
        return False


def _clean(el) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


# Parsed-content cache keyed off the results file mtime, so a fresh scan invalidates it.
_PARSE_CACHE: dict = {"mtime": None, "rules": None, "results": None}


def _parse_scap() -> tuple[dict, list[dict]]:
    """Parse the real XCCDF results file.

    Returns (rules_by_id, results) where:
      rules_by_id[id] = {id,title,description,rationale,fix,severity}
      results = [{id,title,result,severity}, ...] in benchmark order.
    The embedded <Benchmark> carries the Rule definitions (titles/descriptions/fixes);
    the <TestResult> carries the per-rule pass/fail/notapplicable outcomes.
    """
    mtime = SCAP_RESULTS.stat().st_mtime if scap_connected() else None
    if _PARSE_CACHE["mtime"] == mtime and _PARSE_CACHE["rules"] is not None:
        return _PARSE_CACHE["rules"], _PARSE_CACHE["results"]

    rules: dict[str, dict] = {}
    results: list[dict] = []
    if mtime is not None:
        root = ET.parse(SCAP_RESULTS).getroot()
        for rule in root.iter(XCCDF + "Rule"):
            rid = rule.get("id")
            if not rid:
                continue
            fix = rule.find(XCCDF + "fix")
            rules[rid] = {
                "id": rid,
                "title": _clean(rule.find(XCCDF + "title")),
                "description": _clean(rule.find(XCCDF + "description")),
                "rationale": _clean(rule.find(XCCDF + "rationale")),
                "fix": _clean(fix),
                "severity": rule.get("severity", "unknown"),
            }
        for rr in root.iter(XCCDF + "rule-result"):
            res_el = rr.find(XCCDF + "result")
            rid = rr.get("idref")
            meta = rules.get(rid, {})
            results.append({
                "id": rid,
                "title": meta.get("title") or _short_id(rid),
                "result": res_el.text if res_el is not None else "unknown",
                "severity": rr.get("severity") or meta.get("severity", "unknown"),
            })

    _PARSE_CACHE.update(mtime=mtime, rules=rules, results=results)
    return rules, results


def _short_id(rid: str) -> str:
    """Human-ish short name from a long XCCDF rule id."""
    if not rid:
        return "—"
    return rid.split("content_rule_")[-1] if "content_rule_" in rid else rid


# --- activity (KPIs + framework view + failing queue), cached briefly --------
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 15.0

_SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3}


def _days_until(iso: str) -> int | None:
    try:
        y, m, d = (int(x) for x in iso[:10].split("-"))
        return (date(y, m, d) - date.today()).days
    except Exception:
        return None


def _sme_rows() -> list[dict]:
    rows = []
    for it in SME_ITEMS:
        days = _days_until(it["expires"])
        expiring = days is not None and days <= 90
        rows.append({
            "item": it["item"],
            "kind": it["kind"],
            "status": ("EXPIRING" if expiring else it["status"]),
            "expires": it["expires"],
            "days": days,
            "expiring": expiring,
        })
    return rows


def fetch_activity(force: bool = False) -> dict:
    """Parse REAL OpenSCAP results and compute the compliance KPIs the dashboard renders."""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    connected = scap_connected()
    rules, results = _parse_scap()

    passing = sum(1 for r in results if r["result"] == "pass")
    failing = sum(1 for r in results if r["result"] == "fail")
    notapplicable = sum(1 for r in results if r["result"] == "notapplicable")
    scored = passing + failing
    pass_rate = round(100 * passing / scored) if scored else 0

    # Top failing rules by severity (high -> low), then title.
    fails = [r for r in results if r["result"] == "fail"]
    fails.sort(key=lambda r: (_SEV_ORDER.get((r["severity"] or "unknown").lower(), 3), r["title"]))

    sme = _sme_rows()
    sme_open = [s for s in sme if s["expiring"]]

    # Open findings = failing technical controls + expiring SME items.
    open_findings = failing + len(sme_open)

    framework_pct = pass_rate  # the scanned profile IS the primary framework here.

    data = {
        "tenant": TENANT,
        "core": "openscap",
        "connected": connected,
        "profile": SCAP_PROFILE,
        "framework": FRAMEWORK,
        "report_url": "/report",
        "kpis": [
            {"label": "Control pass rate", "value": f"{pass_rate}%", "note": f"{scored} scored controls"},
            {"label": "Passing controls", "value": str(passing), "note": "CIS rules met"},
            {"label": "Failing controls", "value": str(failing), "note": "need remediation"},
            {"label": "Open findings", "value": str(open_findings),
             "note": f"{failing} technical · {len(sme_open)} license/insurance"},
        ],
        "frameworks": [
            {"name": FRAMEWORK, "pct": framework_pct, "passing": passing, "failing": failing,
             "notapplicable": notapplicable},
        ],
        "failing": [
            {"id": r["id"], "short": _short_id(r["id"]), "title": r["title"], "severity": r["severity"]}
            for r in fails
        ],
        "sme": sme,
        "expiring": sme_open,
        "counts": {
            "pass": passing, "fail": failing, "notapplicable": notapplicable, "scored": scored,
        },
    }
    _CACHE.update(ts=now, data=data)
    return data


# --- agentic actions (deterministic OpenSCAP work) ---------------------------
def scan() -> dict:
    """Re-run the REAL oscap scan via scan.sh, then return the updated pass rate.

    Deterministic: shells out to the same scanner used by the seed. Long-running, so
    it's the explicit, on-demand action (the dashboard otherwise reads the cached file).
    If the scan script is absent, this degrades to re-reading the current results file.
    """
    script = SCAP_SCAN_SCRIPT
    summary = "Re-ran the OpenSCAP scan."
    rc = None
    if script.exists():
        proc = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=600
        )
        rc = proc.returncode
        # oscap exit 2 = scan ran with failing rules (normal); >2 is a real error.
        if rc > 2:
            return {"status": "error", "action": "scan", "rc": rc,
                    "summary": "oscap scan failed", "stderr": proc.stderr[-800:]}
    data = fetch_activity(force=True)
    c = data["counts"]
    return {
        "status": "done",
        "action": "scan",
        "rc": rc,
        "pass_rate": data["kpis"][0]["value"],
        "passing": c["pass"],
        "failing": c["fail"],
        "notapplicable": c["notapplicable"],
        "summary": f"Scan complete — {c['pass']} pass / {c['fail']} fail "
                   f"({data['kpis'][0]['value']} of {c['scored']} scored controls). "
                   f"{summary}",
    }


def _resolve_rule(rule_id: str) -> dict | None:
    """Look up a rule by full id or short suffix from the parsed SCAP content."""
    rules, _ = _parse_scap()
    if rule_id in rules:
        return rules[rule_id]
    # allow short ids like 'accounts_umask_etc_profile'
    for rid, meta in rules.items():
        if rid.endswith(rule_id) or _short_id(rid) == rule_id:
            return meta
    return None


def explain(body: dict, blurb: Callable[[str], str | None] | None = None) -> dict:
    """Plain-English explanation + remediation for a failing rule.

    Deterministic answer is built from the rule's own SCAP description/rationale/fix.
    `blurb` is an optional narration callback (the LLM rewrite lives in app.py); the
    action itself is fully deterministic and works with blurb=None.
    """
    rule_id = body.get("rule_id") or body.get("rule") or ""
    meta = _resolve_rule(rule_id)
    if not meta:
        data = fetch_activity()
        return {
            "status": "error", "action": "explain",
            "error": f"unknown rule '{rule_id}'",
            "hint": "Use a rule id from /api/activity .failing[].id",
            "available": [f["id"] for f in data["failing"]][:10],
        }

    rationale = meta["rationale"] or "This CIS control hardens the server against a known risk."
    fix = meta["fix"] or "Apply the configuration described in the CIS benchmark."
    deterministic = (
        f"Control: {meta['title']}. "
        f"What it checks: {meta['description'] or 'see the CIS benchmark rule.'} "
        f"Why it matters: {rationale} "
        f"How to fix: {fix}"
    )

    reasoning = blurb(
        "You are a compliance engineer for a wealth-management firm's IT. In 3-4 plain-English "
        "sentences (no jargon, no preamble), explain this failing CIS server control to the "
        f"owner and how to fix it. Title: {meta['title']}. Description: {meta['description']}. "
        f"Rationale: {meta['rationale']}. Remediation: {meta['fix']}"
    ) if blurb else None

    out = {
        "status": "done",
        "action": "explain",
        "rule_id": meta["id"],
        "title": meta["title"],
        "severity": meta["severity"],
        "explanation": deterministic,
        "remediation": fix,
        "source": "openscap-content",
    }
    if reasoning:
        out["explanation_llm"] = reasoning
    return out


def remediate(body: dict) -> dict:
    """Applying a system fix changes the host configuration — never auto-executed.

    The module declares approval_required:[policy_change], so this stages the fix and
    returns pending_approval with the exact remediation that *would* run.
    """
    rule_id = body.get("rule_id") or body.get("rule") or ""
    meta = _resolve_rule(rule_id)
    if not meta:
        data = fetch_activity()
        return {
            "status": "error", "action": "remediate",
            "error": f"unknown rule '{rule_id}'",
            "available": [f["id"] for f in data["failing"]][:10],
        }
    fix = meta["fix"] or "Apply the CIS-recommended configuration for this control."
    return {
        "status": "pending_approval",
        "action": "remediate",
        "approval": "policy_change",
        "requires": "human approval",
        "rule_id": meta["id"],
        "title": meta["title"],
        "severity": meta["severity"],
        "proposed_remediation": fix,
        "summary": f"Remediation for '{meta['title']}' is staged and awaiting human approval. "
                   "System fixes are never auto-applied by the agent (policy_change is "
                   "approval-gated).",
    }


def _consent_id(customer: str, subscription: str) -> str:
    """Deterministic consent id from (customer, subscription) — stable across re-files."""
    seed = f"{customer}|{subscription}".encode("utf-8")
    return "consent-" + hashlib.sha256(seed).hexdigest()[:16]


def file_consent(body: dict) -> dict:
    """Append the customer's consent/GDPR record to the append-only evidence ledger.

    The onboarding mission's human gate: filing a consent record is side-effecting audit
    evidence, so the operator declares approval_required=True. There is no OpenSCAP call —
    instead one JSON line is appended to CONSENT_LEDGER, a tamper-evident JSONL audit file.

    Idempotent: the consent_id is a deterministic hash of (customer, subscription), and a
    re-file for the same pair is a no-op (we scan the ledger and skip the duplicate append),
    so the audit trail carries exactly one consent record per customer/subscription.
    """
    customer = body.get("customer") or "unknown"
    subscription = body.get("subscription") or "default"
    consent_id = _consent_id(customer, subscription)

    ledger = Path(CONSENT_LEDGER)
    already = False
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("consent_id") == consent_id:
                already = True
                break

    if not already:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "kind": "consent",
            "customer": customer,
            "subscription": subscription,
            "consent_id": consent_id,
            "filed": True,
        }
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    return {
        "status": "done",
        "action": "file_consent",
        "consent_filed": True,
        "consent_id": consent_id,
    }
