#!/usr/bin/env python3
"""Repeatable seeder for the Summit Roofing Co. demo tenant on self-hosted Chatwoot.

Bootstrap method (the reliable one for self-hosted Chatwoot, mirroring the Lago
reference): run a Ruby script via `rails runner` inside the Chatwoot rails container.
That creates/updates — idempotently — a super-admin user, the account
"Summit Roofing Co.", an API inbox, a couple of contacts, and ~6-8 roofing-support
conversations across open / pending / resolved.

Chatwoot's API needs an agent **access token** (the `access_token.token` on a User).
This seed reads that generated token back and prints `ACCESS_TOKEN=<value>` +
`ACCOUNT_ID=<id>`; we capture both and write agents/support/.env so app.py picks them
up with no manual copy/paste.

Usage:
    python3 seed.py
    CHATWOOT_CONTAINER=agentic-chatwoot-rails-1 python3 seed.py

Env knobs:
    CHATWOOT_CONTAINER  docker container of the Chatwoot rails app
                        (default: agentic-chatwoot-rails-1)
    CHATWOOT_API_URL    REST base used by app.py (default: http://localhost:3003)
    CHATWOOT_FRONT_URL  Chatwoot UI link baked into .env (default: http://localhost:3003)
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

CONTAINER = os.environ.get("CHATWOOT_CONTAINER", "agentic-chatwoot-rails-1")
CHATWOOT_API_URL = os.environ.get("CHATWOOT_API_URL", "http://localhost:3003")
CHATWOOT_FRONT_URL = os.environ.get("CHATWOOT_FRONT_URL", "http://localhost:3003")

# `sudo` is required to talk to the docker socket on this host.
DOCKER = ["sudo", "docker"]
IN_CONTAINER_PATH = "/tmp/summit_support_seed.rb"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def main() -> int:
    if not SEED_RB.exists():
        print(f"seed.rb not found at {SEED_RB}", file=sys.stderr)
        return 1

    # 1. Copy the Ruby seed into the running Chatwoot rails container.
    cp = run(DOCKER + ["cp", str(SEED_RB), f"{CONTAINER}:{IN_CONTAINER_PATH}"])
    if cp.returncode != 0:
        print("docker cp failed:\n" + cp.stderr, file=sys.stderr)
        return 1

    # 2. Run it with rails runner (the Chatwoot rails app).
    res = run(DOCKER + ["exec", CONTAINER, "bundle", "exec", "rails", "runner", IN_CONTAINER_PATH])
    out = res.stdout + "\n" + res.stderr

    seed_ok = re.search(r"^SEED_OK .*$", out, re.MULTILINE)
    tok_match = re.search(r"^ACCESS_TOKEN=(\S+)$", out, re.MULTILINE)
    acct_match = re.search(r"^ACCOUNT_ID=(\S+)$", out, re.MULTILINE)
    if not (seed_ok and tok_match and acct_match):
        print("Seeding did not report success. Output:\n" + out, file=sys.stderr)
        return 1

    token = tok_match.group(1)
    account_id = acct_match.group(1)
    print(seed_ok.group(0))
    print(f"ACCOUNT_ID={account_id}")
    print(f"ACCESS_TOKEN={token}")

    # 3. Persist the env so app.py picks up the live token + account automatically.
    ENV_OUT.write_text(
        f"CHATWOOT_API_URL={CHATWOOT_API_URL}\n"
        f"CHATWOOT_API_TOKEN={token}\n"
        f"CHATWOOT_ACCOUNT_ID={account_id}\n"
        f"CHATWOOT_FRONT_URL={CHATWOOT_FRONT_URL}\n"
    )
    print(f"Wrote {ENV_OUT} (CHATWOOT_API_URL, CHATWOOT_API_TOKEN, CHATWOOT_ACCOUNT_ID, CHATWOOT_FRONT_URL)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
