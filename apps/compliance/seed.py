#!/usr/bin/env python3
"""Repeatable seeder for the agentic-compliance module.

Unlike the billing core (a long-running Lago server), the "core" here is the
**OpenSCAP scanner** producing REAL results that the agent monitors/explains.
Seeding therefore means: run a real `oscap` scan once (idempotent, cached) and
write agents/compliance/.env so app.py knows where the results live.

What it does:
  1. Runs scan.sh, which fetches the ComplianceAsCode SCAP Security Guide
     datastream (ssg-ubuntu2204-ds.xml) if absent, then runs a real CIS Ubuntu
     22.04 Level 1 - Server scan in a disposable container, writing
     results/scan-results.xml + results/report.html.
  2. Writes .env (SCAP_RESULTS, SCAP_REPORT, SCAP_PROFILE) for app.py.

Idempotent: re-running re-scans and overwrites the cached results. By default it
skips the scan if a results file already exists (pass --force / FORCE=1 to rescan).

Usage:
    python3 seed.py                 # scan only if results missing
    python3 seed.py --force         # always re-scan
    SCAP_PROFILE=... python3 seed.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCAN_SH = HERE / "scan.sh"
RESULTS = HERE / "results" / "scan-results.xml"
REPORT = HERE / "results" / "report.html"
ENV_OUT = HERE / ".env"

SCAP_PROFILE = os.environ.get(
    "SCAP_PROFILE", "xccdf_org.ssgproject.content_profile_cis_level1_server"
)


def run_scan() -> int:
    if not SCAN_SH.exists():
        print(f"scan.sh not found at {SCAN_SH}", file=sys.stderr)
        return 1
    env = dict(os.environ, SCAP_PROFILE=SCAP_PROFILE)
    proc = subprocess.run(["bash", str(SCAN_SH)], env=env)
    return proc.returncode


def main() -> int:
    force = "--force" in sys.argv[1:] or os.environ.get("FORCE") == "1"

    if RESULTS.exists() and not force:
        print(f"Cached scan present at {RESULTS} (use --force to re-scan).")
    else:
        print("Running real OpenSCAP scan via scan.sh ...")
        rc = run_scan()
        if rc != 0 or not RESULTS.exists():
            print(f"Scan did not produce results (rc={rc}).", file=sys.stderr)
            return 1

    ENV_OUT.write_text(
        f"SCAP_RESULTS={RESULTS}\n"
        f"SCAP_REPORT={REPORT}\n"
        f"SCAP_PROFILE={SCAP_PROFILE}\n"
    )
    print(f"Wrote {ENV_OUT} (SCAP_RESULTS, SCAP_REPORT, SCAP_PROFILE)")

    # Tiny summary so the seed echoes real numbers (like billing's SEED_OK line).
    try:
        text = RESULTS.read_text(errors="ignore")
        p = text.count("<result>pass</result>")
        f = text.count("<result>fail</result>")
        na = text.count("<result>notapplicable</result>")
        rate = round(100 * p / (p + f)) if (p + f) else 0
        print(f"SEED_OK profile=cis_level1_server pass={p} fail={f} "
              f"notapplicable={na} pass_rate={rate}%")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
