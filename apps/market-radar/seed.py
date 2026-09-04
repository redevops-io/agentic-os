#!/usr/bin/env python3
"""Repeatable seeder for the Meridian Wealth Management demo tenant on self-hosted
changedetection.io.

Bootstrap method (the reliable one for changedetection): the REST API key is NOT
created by us — changedetection generates it on first boot and stores it in its
datastore. We read it back, then create the demo watches over the REST API.

  1. Read the api token from the container's datastore JSON:
       sudo docker exec <container> cat /datastore/url-watches.json   (older builds)
       sudo docker exec <container> cat /datastore/changedetection.json (0.45+)
     -> settings.application.api_access_token
  2. Confirm with GET /api/v1/systeminfo using header `x-api-key: <token>`.
  3. POST /api/v1/watch for ~5 wealth-management competitor / fee / regulatory / market-data pages
     (idempotent: existing watches with the same URL are skipped).
  4. Write agents/market-radar/.env so app.py picks up CD_API_KEY automatically.

The watches won't have real diffs yet — that's fine; the dashboard shows the watch
list, last-checked, and any change state.

Usage:
    python3 seed.py
    CD_CONTAINER=agentic-cores-changedetection-1 python3 seed.py

Env knobs:
    CD_CONTAINER  docker container name (default: agentic-cores-changedetection-1)
    CD_API_URL    REST base used for seeding + verification (default: http://localhost:5001)
    CD_FRONT_URL  changedetection UI link baked into .env (default: http://localhost:5001)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_OUT = HERE / ".env"

CONTAINER = os.environ.get("CD_CONTAINER", "agentic-cores-changedetection-1")
CD_API_URL = os.environ.get("CD_API_URL", "http://localhost:5001").rstrip("/")
CD_FRONT_URL = os.environ.get("CD_FRONT_URL", "http://localhost:5001")

# `sudo` is required to talk to the docker socket on this host.
DOCKER = ["sudo", "docker"]

# changedetection stores its settings in one of these files depending on version.
DATASTORE_FILES = [
    "/datastore/changedetection.json",
    "/datastore/url-watches.json",
]

# 5 wealth-management competitive-intelligence watches. tag is a comma-separated string of
# tag titles (changedetection auto-creates the tags). Stable public URLs so the
# watches actually fetch on this host.
WATCHES = [
    {"title": "Vanguard Personal Advisor — fees", "tag": "competitor,pricing",
     "url": "https://investor.vanguard.com/advice/personal-advisor"},
    {"title": "Fisher Investments — services", "tag": "competitor",
     "url": "https://www.fisherinvestments.com/en-us"},
    {"title": "Betterment — pricing", "tag": "competitor,pricing",
     "url": "https://www.betterment.com/pricing"},
    {"title": "Custodian fee schedule index", "tag": "pricing",
     "url": "https://www.schwab.com/pricing"},
    {"title": "SEC investment-adviser updates", "tag": "regulatory",
     "url": "https://www.sec.gov/newsroom/press-releases"},
]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def read_api_token() -> str | None:
    """Read settings.application.api_access_token from the container datastore."""
    for path in DATASTORE_FILES:
        res = run(DOCKER + ["exec", CONTAINER, "cat", path])
        if res.returncode != 0:
            continue
        try:
            doc = json.loads(res.stdout)
        except Exception:
            continue
        token = (doc.get("settings", {}).get("application", {})
                 .get("api_access_token"))
        if token:
            print(f"Found api_access_token in {path} (container {CONTAINER})")
            return token
    return None


def api(method: str, path: str, token: str, payload: dict | None = None):
    url = f"{CD_API_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-api-key", token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode()
        return resp.status, (json.loads(body) if body.strip() else {})


def main() -> int:
    token = read_api_token()
    if not token:
        print("Could not read api_access_token from the container datastore.",
              file=sys.stderr)
        return 1

    # Confirm the key works against the live REST API.
    try:
        status, info = api("GET", "/api/v1/systeminfo", token)
    except urllib.error.URLError as e:
        print(f"GET /api/v1/systeminfo failed: {e}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"systeminfo returned HTTP {status} — bad key?", file=sys.stderr)
        return 1
    print(f"systeminfo OK: version={info.get('version')} "
          f"watch_count={info.get('watch_count')}")

    # Existing watch URLs (idempotency).
    _, existing = api("GET", "/api/v1/watch", token)
    have = {w.get("url") for w in (existing.values() if isinstance(existing, dict) else [])}

    created = 0
    for w in WATCHES:
        if w["url"] in have:
            print(f"  skip (exists): {w['title']}")
            continue
        try:
            st, resp = api("POST", "/api/v1/watch", token, w)
            if st in (200, 201):
                created += 1
                print(f"  created: {w['title']} -> {resp.get('uuid')}")
            else:
                print(f"  FAILED ({st}): {w['title']}", file=sys.stderr)
        except urllib.error.HTTPError as e:
            print(f"  FAILED ({e.code}): {w['title']} — {e.read().decode()[:160]}",
                  file=sys.stderr)

    _, after = api("GET", "/api/v1/watch", token)
    total = len(after) if isinstance(after, dict) else 0
    print(f"SEED_OK watches_created={created} total_watches={total}")

    ENV_OUT.write_text(
        f"CD_API_URL={CD_API_URL}\n"
        f"CD_API_KEY={token}\n"
        f"CD_FRONT_URL={CD_FRONT_URL}\n"
    )
    print(f"Wrote {ENV_OUT} (CD_API_URL, CD_API_KEY, CD_FRONT_URL)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
