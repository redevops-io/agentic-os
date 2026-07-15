#!/usr/bin/env python3
"""Repeatable seeder + bootstrap for the Summit Roofing Co. SOC demo on CrowdSec.

Bootstrap method (the reliable one for self-hosted CrowdSec): everything is driven
through `cscli` inside the running CrowdSec container via `sudo docker exec`.

  1. Create a bouncer credential — `cscli bouncers add summit-agent -o raw` — and
     capture the API key. app.py reads live decisions with it over the LAPI
     (GET /v1/decisions, header X-Api-Key). Idempotent: if the bouncer already
     exists we delete + re-add to recover a usable key.
  2. Inject believable Summit Roofing security activity with `cscli decisions add`
     (each also creates an alert): SSH bruteforce, port scans against the office NVR,
     a web path-traversal probe, WordPress login bruteforce, an nmap SYN scan, and a
     credential-stuffing botnet range — varied scenarios + severities + durations.
  3. Write agents/edge-sentinel/.env so app.py picks up CROWDSEC_BOUNCER_KEY with no
     manual copy/paste.

Usage:
    python3 seed.py
    CROWDSEC_CONTAINER=agentic-cores-crowdsec-1 python3 seed.py

Env knobs:
    CROWDSEC_CONTAINER  docker container name (default: agentic-cores-crowdsec-1)
    CROWDSEC_LAPI_URL   LAPI base for the .env (default: http://localhost:8086)
    CROWDSEC_FRONT_URL  metrics/LAPI link baked into the .env (default: http://localhost:8086)
    BOUNCER_NAME        bouncer credential name (default: summit-agent)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_OUT = HERE / ".env"

CONTAINER = os.environ.get("CROWDSEC_CONTAINER", "agentic-cores-crowdsec-1")
CROWDSEC_LAPI_URL = os.environ.get("CROWDSEC_LAPI_URL", "http://localhost:8086")
CROWDSEC_FRONT_URL = os.environ.get("CROWDSEC_FRONT_URL", "http://localhost:8086")
BOUNCER_NAME = os.environ.get("BOUNCER_NAME", "summit-agent")

# `sudo` is required to talk to the docker socket on this host.
DOCKER = ["sudo", "docker"]

# Varied, believable Summit Roofing edge-security activity.
# (ip|range, duration, reason incl. crowdsecurity/<scenario> token, type)
SEED_DECISIONS = [
    ("--ip", "45.143.200.14", "4h", "ssh bruteforce (crowdsecurity/ssh-bf)"),
    ("--ip", "185.220.101.34", "6h", "port scan against office NVR (crowdsecurity/port-scan)"),
    ("--ip", "89.248.165.52", "2h", "http path traversal probe (crowdsecurity/http-probing)"),
    ("--ip", "193.41.206.18", "12h", "wordpress login bruteforce (crowdsecurity/wp-bf)"),
    ("--ip", "23.129.64.130", "1h", "nmap TCP SYN scan (crowdsecurity/port-scan)"),
    ("--range", "141.98.10.0/24", "8h", "credential stuffing botnet (crowdsecurity/http-bf)"),
]


def cscli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        DOCKER + ["exec", CONTAINER, "cscli", *args],
        text=True, capture_output=True,
    )


def bootstrap_bouncer() -> str | None:
    """Create (or re-create) the bouncer credential and return its raw API key."""
    res = cscli(["bouncers", "add", BOUNCER_NAME, "-o", "raw"])
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip().splitlines()[-1].strip()

    # Already exists → delete and re-add so we get a fresh, usable key.
    if "already exists" in (res.stdout + res.stderr).lower():
        cscli(["bouncers", "delete", BOUNCER_NAME])
        res = cscli(["bouncers", "add", BOUNCER_NAME, "-o", "raw"])
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().splitlines()[-1].strip()

    print("Failed to add bouncer:\n" + res.stdout + res.stderr, file=sys.stderr)
    return None


def inject_decisions() -> int:
    added = 0
    for scope_flag, value, duration, reason in SEED_DECISIONS:
        res = cscli([
            "decisions", "add", scope_flag, value,
            "--duration", duration, "--reason", reason, "--type", "ban",
        ])
        if res.returncode == 0:
            added += 1
        else:
            print(f"  warn: could not add {value}: {res.stderr.strip()}", file=sys.stderr)
    return added


def count_state() -> tuple[int, int]:
    dres = cscli(["decisions", "list", "-o", "json"])
    ares = cscli(["alerts", "list", "-o", "json"])
    def _n(s: str) -> int:
        try:
            return len(json.loads(s) or [])
        except Exception:
            return 0
    return _n(dres.stdout), _n(ares.stdout)


def main() -> int:
    # 0. sanity: container reachable?
    if cscli(["version"]).returncode != 0:
        print(f"CrowdSec container '{CONTAINER}' not reachable via docker exec.", file=sys.stderr)
        return 1

    # 1. bootstrap bouncer credential
    key = bootstrap_bouncer()
    if not key:
        return 1
    print(f"BOUNCER_KEY={key}")

    # 2. inject believable activity
    added = inject_decisions()
    decisions, alerts = count_state()
    print(f"SEED_OK bouncer={BOUNCER_NAME} added={added} decisions={decisions} alerts={alerts}")

    # 3. persist env so app.py picks up the live key automatically
    ENV_OUT.write_text(
        f"CROWDSEC_LAPI_URL={CROWDSEC_LAPI_URL}\n"
        f"CROWDSEC_BOUNCER_KEY={key}\n"
        f"CROWDSEC_CONTAINER={CONTAINER}\n"
        f"CROWDSEC_FRONT_URL={CROWDSEC_FRONT_URL}\n"
    )
    print(f"Wrote {ENV_OUT} (CROWDSEC_LAPI_URL, CROWDSEC_BOUNCER_KEY, CROWDSEC_CONTAINER, CROWDSEC_FRONT_URL)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
