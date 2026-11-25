"""Report a JUnit XML result file to the dashboards (``POST /api/dashboards/qa/results``).

    python -m agentic_os.dashboards.report_junit results.xml --url https://projects.example \\
        --project storefront --suite checkout --platform chromium --run-id "$GITHUB_RUN_ID" [--environment staging]

The service token is read from ``DASHBOARDS_SERVICE_TOKEN`` (never from the command line). Mapping, stated so nobody has
to guess: a testcase with a ``failure`` or ``error`` is ``failed``; a ``skipped`` one is ``unsupported`` when its message
says the platform cannot run it or it does not apply (``unsupported``, ``not supported``, ``not applicable``, ``not available on``, ``requires``) and ``blocked``
otherwise; anything else is ``passed``. A test that is not in the file is not reported, and the dashboards then count it
as not executed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List

_UNSUPPORTED = re.compile(r"unsupported|not supported|not applicable|not available on|requires |only on |platform", re.I)


def parse_junit(path: str, only: str = "", exclude: str = "") -> List[Dict[str, Any]]:
    """``only`` / ``exclude`` are regular expressions on the test id, so one file that covers several platforms (a
    parametrised browser matrix) can be reported once per platform."""
    root = ET.parse(path).getroot()      # noqa: S314  (a local file the operator's own CI produced)
    out: Dict[str, Dict[str, Any]] = {}
    keep, drop = (re.compile(only) if only else None), (re.compile(exclude) if exclude else None)
    for tc in root.iter("testcase"):
        name = " :: ".join(x for x in (tc.get("classname") or tc.get("file") or "", tc.get("name") or "") if x)[:400]
        if not name or (keep and not keep.search(name)) or (drop and drop.search(name)):
            continue
        status, detail = "passed", ""
        for child in tc:
            tag = child.tag.lower()
            if tag in ("failure", "error"):
                status, detail = "failed", (child.get("message") or (child.text or "").strip().splitlines()[0] if (child.get("message") or child.text) else "")
                break
            if tag == "skipped":
                msg = child.get("message") or (child.text or "")
                status, detail = ("unsupported" if _UNSUPPORTED.search(msg) else "blocked"), msg
        try:
            dur = float(tc.get("time") or 0) * 1000.0
        except ValueError:
            dur = None
        prev = out.get(name)
        if prev is None or (prev["status"] == "passed" and status != "passed"):       # a repeated id keeps its worst result
            out[name] = {"test_id": name, "status": status, "detail": str(detail)[:500], "duration_ms": dur}
    return list(out.values())


def main(argv: "List[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("junit")
    for a in ("url", "project", "suite", "run-id"):
        ap.add_argument(f"--{a}", required=True)
    for a, d in (("platform", "default"), ("environment", ""), ("product", ""), ("origin", "ci"), ("ref", ""), ("link", ""), ("release", "")):
        ap.add_argument(f"--{a}", default=d)
    ap.add_argument("--at", type=float, default=0.0, help="epoch seconds the run finished (default: now)")
    ap.add_argument("--only", default="", help="report only test ids matching this regular expression")
    ap.add_argument("--exclude", default="", help="leave out test ids matching this regular expression")
    ap.add_argument("--evidence-verified", choices=("yes", "no"), default="", help="state whether the run's evidence was re-verified independently")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    results = parse_junit(a.junit, a.only, a.exclude)
    if not results:
        print("no testcase found in the file", file=sys.stderr)
        return 2
    body = {"project": a.project, "suite": a.suite, "run_id": a.run_id, "platform": a.platform, "environment": a.environment, "product": a.product,
            "origin": a.origin, "ref": a.ref, "url": a.link, "at": a.at or time.time(), "results": results}
    if a.release:
        body["release"] = a.release
    if a.evidence_verified:
        body["evidence_verified"] = a.evidence_verified == "yes"
    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if a.dry_run:
        print(json.dumps({"would_report": {k: body[k] for k in ("project", "suite", "run_id", "platform")}, "counts": counts}))
        return 0
    token = os.environ.get("DASHBOARDS_SERVICE_TOKEN", "")
    if not token:
        print("DASHBOARDS_SERVICE_TOKEN is not set", file=sys.stderr)
        return 2
    headers = {"Content-Type": "application/json", "X-Service-Token": token,
               # a named client: some edges refuse the interpreter's default identity outright
               "User-Agent": "agentic-os-dashboards-reporter/1"}
    # A deployment behind the sign-in gate (PROJECTS_OPERATOR_GATE) answers 401 to anything without an Authorization it knows, before
    # the service token is even looked at. DASHBOARDS_AUTHORIZATION is that header's whole value ("Bearer ..." or "Basic ..."); the
    # service token still decides whether the run may be reported.
    gate = os.environ.get("DASHBOARDS_AUTHORIZATION", "").strip()
    if gate:
        headers["Authorization"] = gate
    req = urllib.request.Request(a.url.rstrip("/") + "/api/dashboards/qa/results", data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:      # noqa: S310  (the operator's own Projects URL)
            print(json.dumps({**json.loads(r.read() or b"{}"), "counts": counts}))
            return 0
    except urllib.error.HTTPError as e:
        print(f"refused ({e.code}): {e.read().decode('utf-8', 'replace')[:400]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
