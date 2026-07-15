#!/usr/bin/env python3
"""Repeatable seeder for the Summit Roofing Co. demo tenant on self-hosted Lago v1.48.

Bootstrap method (the reliable one for self-hosted Lago): copy seed.rb into the Lago
API/Rails container and run it with `rails runner`. That creates/updates the org, a
billing entity, an API key, customers, plans, and invoices — all idempotently.

The Ruby script prints `API_KEY=<value>` on success; we capture it and write
agents/billing/.env so app.py can read LAGO_API_KEY without any manual copy/paste.

Usage:
    python3 seed.py                      # uses defaults below
    LAGO_API_CONTAINER=lago-api python3 seed.py

Env knobs:
    LAGO_API_CONTAINER  docker container name of the Lago Rails app (default: lago-api)
    LAGO_API_URL        REST base used for the post-seed verification (default: http://localhost:3000)
    LAGO_FRONT_URL      Lago UI link baked into the .env (default: http://localhost:80)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED_RB = HERE / "seed.rb"
ENV_OUT = HERE / ".env"

CONTAINER = os.environ.get("LAGO_API_CONTAINER", "lago-api")
LAGO_API_URL = os.environ.get("LAGO_API_URL", "http://localhost:3000")
LAGO_FRONT_URL = os.environ.get("LAGO_FRONT_URL", "http://localhost:80")

# `sudo` is required to talk to the docker socket on this host.
DOCKER = ["sudo", "docker"]
IN_CONTAINER_PATH = "/tmp/summit_seed.rb"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def main() -> int:
    if not SEED_RB.exists():
        print(f"seed.rb not found at {SEED_RB}", file=sys.stderr)
        return 1

    # 1. Copy the Ruby seed into the running Lago API container.
    cp = run(DOCKER + ["cp", str(SEED_RB), f"{CONTAINER}:{IN_CONTAINER_PATH}"])
    if cp.returncode != 0:
        print("docker cp failed:\n" + cp.stderr, file=sys.stderr)
        return 1

    # 2. Run it with rails runner (the Lago Rails app).
    res = run(DOCKER + ["exec", CONTAINER, "bundle", "exec", "rails", "runner", IN_CONTAINER_PATH])
    out = res.stdout + "\n" + res.stderr

    seed_ok = re.search(r"^SEED_OK .*$", out, re.MULTILINE)
    key_match = re.search(r"^API_KEY=(\S+)$", out, re.MULTILINE)
    if not (seed_ok and key_match):
        print("Seeding did not report success. Output:\n" + out, file=sys.stderr)
        return 1

    api_key = key_match.group(1)
    print(seed_ok.group(0))
    print(f"API_KEY={api_key}")

    # 3. Persist the env so app.py picks up the live key automatically.
    ENV_OUT.write_text(
        f"LAGO_API_URL={LAGO_API_URL}\n"
        f"LAGO_API_KEY={api_key}\n"
        f"LAGO_FRONT_URL={LAGO_FRONT_URL}\n"
    )
    print(f"Wrote {ENV_OUT} (LAGO_API_URL, LAGO_API_KEY, LAGO_FRONT_URL)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
