#!/usr/bin/env python3
"""Repeatable seeder for the Summit Roofing Co. demo tenant on self-hosted Postiz.

Why we seed the database directly (and not the REST API):
    This Postiz build (ghcr.io/gitroomhq/postiz-app:latest) boots a NestJS backend
    that, on `onModuleInit`, tries to connect to a **Temporal** server at
    `127.0.0.1:7233`. That Temporal service is NOT deployed in this stack, so the
    backend throws during init and **never finishes `app.listen()`** — nothing binds
    the HTTP API port (3000). nginx proxies `/api/ -> localhost:3000`, so every
    `/api/...` call (register, login, public API) returns 502 / connection refused.
    The frontend (Next.js, :4200) and the orchestrator (:3002) are up, but the REST
    API the agent would read is unreachable.

    The prompt explicitly authorises seeding the Postiz postgres directly to create a
    user + real scheduled posts. That is the path that works here, and the one this
    script uses. The GOAL — real "scheduled posts" rows the agent/dashboard can read —
    is fully met: the agent (app.py) reads these rows straight from postgres.

What it creates (idempotent — safe to re-run):
    * 1 Organization  "Summit Roofing Co." (with a stable apiKey)
    * 1 User          agent@summitroofing.co / SummitRoof!2026  (bcrypt, can log in
                      via the Postiz UI once a Temporal server is added to the stack)
    * 1 UserOrganization (role SUPERADMIN)
    * 3 Integrations  Instagram / Facebook / Google Business Profile (modelled as
                      connected channels; tokens are placeholders since no real social
                      account is linked) with follower counts in `audience`/profile
    * 5 Posts         the roofing-SME content calendar (QUEUE / DRAFT states), spread
                      across the next 7 days + one PUBLISHED

Usage:
    python3 seed.py
    PG_CONTAINER=agentic-postiz-postiz-postgres-1 python3 seed.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_OUT = HERE / ".env"

# `sudo` is required to talk to the docker socket on this host.
DOCKER = ["sudo", "docker"]
PG_CONTAINER = os.environ.get("PG_CONTAINER", "agentic-postiz-postiz-postgres-1")
APP_CONTAINER = os.environ.get("APP_CONTAINER", "agentic-postiz-postiz-1")
PG_USER = os.environ.get("PG_USER", "postiz")
PG_DB = os.environ.get("PG_DB", "postiz")
POSTIZ_FRONT_URL = os.environ.get("POSTIZ_FRONT_URL", "http://localhost:4200")

# Stable identifiers so re-runs upsert the same rows.
ORG_ID = "summit-roofing-org"
ORG_API_KEY = os.environ.get("POSTIZ_API_KEY", "changeme-set-POSTIZ_API_KEY-in-env")
USER_ID = "summit-roofing-user"
USER_EMAIL = "agent@summitroofing.co"
USER_PASSWORD = os.environ.get("POSTIZ_USER_PASSWORD", "changeme-set-in-env")
UORG_ID = "summit-roofing-uorg"

# (id, name, providerIdentifier, followers) — modelled channels.
INTEGRATIONS = [
    ("summit-ig", "Summit Roofing · Instagram", "instagram", 3120),
    ("summit-fb", "Summit Roofing · Facebook", "facebook", 4850),
    ("summit-gbp", "Summit Roofing · Google Business", "google", 980),
]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def psql(sql: str) -> subprocess.CompletedProcess:
    return run(DOCKER + ["exec", "-i", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB, "-v", "ON_ERROR_STOP=1", "-c", sql])


def bcrypt_hash(password: str) -> str:
    """Hash with the SAME bcrypt the Postiz backend uses (cost 10) so the seeded
    user is a real, loginable account."""
    res = run(DOCKER + ["exec", APP_CONTAINER, "node", "-e",
                         f'console.log(require("bcrypt").hashSync({json.dumps(password)},10))'])
    if res.returncode != 0 or not res.stdout.strip().startswith("$2"):
        print("bcrypt hashing failed:\n" + res.stdout + res.stderr, file=sys.stderr)
        sys.exit(1)
    return res.stdout.strip()


def q(s: str) -> str:
    """Single-quote + escape for inline SQL."""
    return "'" + s.replace("'", "''") + "'"


def content_json(text: str) -> str:
    """Postiz stores Post.content as a JSON string of the editor value-array:
    [{"content": "<the post body>"}]. We match that shape."""
    return json.dumps([{"content": text}])


def posts() -> list[dict]:
    """The roofing-SME content calendar — real rows the dashboard renders."""
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def at(days: float, hour: int) -> str:
        d = (now + timedelta(days=days)).replace(hour=hour, minute=0, second=0)
        return d.strftime("%Y-%m-%d %H:%M:%S")

    return [
        {"id": "summit-post-1", "int": "summit-ig", "state": "QUEUE",
         "publish": at(1, 9),
         "text": "Completed: Victorian restoration in Oak Park 📸 Slate roof brought back to life — swipe for the before/after. #OakPark #RoofingDoneRight"},
        {"id": "summit-post-2", "int": "summit-fb", "state": "QUEUE",
         "publish": at(2, 8),
         "text": "5 signs you need a new roof: curling shingles, granules in the gutters, daylight in the attic, sagging, and a roof past 20 years. Book a free inspection."},
        {"id": "summit-post-3", "int": "summit-fb", "state": "QUEUE",
         "publish": at(4, 8),
         "text": "Storm season is coming. Prep tips: clear gutters, trim overhanging branches, check flashing, and schedule a pre-storm inspection. Stay dry, Summit."},
        {"id": "summit-post-4", "int": "summit-ig", "state": "DRAFT",
         "publish": at(5, 10),
         "text": "Before / After: a tired asphalt roof in Maplewood, transformed in 3 days. Which side is your favourite? 🏠✨ (carousel)"},
        {"id": "summit-post-5", "int": "summit-gbp", "state": "QUEUE",
         "publish": at(6, 12),
         "text": "Loved your new roof? A quick Google review helps your neighbours find us — and means the world to our crew. Thank you! ⭐⭐⭐⭐⭐"},
        # One already-published post so the feed shows a 'live' item too.
        {"id": "summit-post-6", "int": "summit-ig", "state": "PUBLISHED",
         "publish": at(-2, 9),
         "text": "Crew spotlight: meet foreman Dave — 14 years keeping Summit roofs watertight. 👷"},
    ]


def build_sql(pw_hash: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    parts: list[str] = ["BEGIN;"]

    # Organization
    parts.append(
        f'INSERT INTO "Organization" (id, name, description, "apiKey", "createdAt", "updatedAt", "allowTrial", "isTrailing", shortlink) '
        f"VALUES ({q(ORG_ID)}, {q('Summit Roofing Co.')}, {q('Roofing SME — agentic social on Postiz')}, {q(ORG_API_KEY)}, {q(now)}, {q(now)}, false, false, 'ASK') "
        'ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, "apiKey"=EXCLUDED."apiKey", "updatedAt"=EXCLUDED."updatedAt";'
    )
    # User (real bcrypt password, SUPERADMIN, audience = headline follower count)
    parts.append(
        f'INSERT INTO "User" (id, email, password, "providerName", name, "isSuperAdmin", audience, timezone, "createdAt", "updatedAt", "lastReadNotifications", activated, "connectedAccount", "lastOnline") '
        f"VALUES ({q(USER_ID)}, {q(USER_EMAIL)}, {q(pw_hash)}, 'LOCAL', {q('Summit Social Agent')}, true, 8950, 0, {q(now)}, {q(now)}, {q(now)}, true, false, {q(now)}) "
        'ON CONFLICT (id) DO UPDATE SET email=EXCLUDED.email, password=EXCLUDED.password, "updatedAt"=EXCLUDED."updatedAt";'
    )
    # Membership
    parts.append(
        f'INSERT INTO "UserOrganization" (id, "userId", "organizationId", disabled, role, "createdAt", "updatedAt") '
        f"VALUES ({q(UORG_ID)}, {q(USER_ID)}, {q(ORG_ID)}, false, 'SUPERADMIN', {q(now)}, {q(now)}) "
        'ON CONFLICT (id) DO UPDATE SET role=EXCLUDED.role, "updatedAt"=EXCLUDED."updatedAt";'
    )
    # Integrations (channels). profile carries follower count for the agent to read.
    for iid, name, prov, followers in INTEGRATIONS:
        profile = json.dumps({"followers": followers, "username": iid})
        parts.append(
            f'INSERT INTO "Integration" (id, "internalId", "organizationId", name, "providerIdentifier", type, token, disabled, profile, "createdAt", "updatedAt", "inBetweenSteps", "refreshNeeded", "postingTimes") '
            f"VALUES ({q(iid)}, {q(iid + '-internal')}, {q(ORG_ID)}, {q(name)}, {q(prov)}, 'social', {q('seed-placeholder-token')}, false, {q(profile)}, {q(now)}, {q(now)}, false, false, {q('[{\"time\":120},{\"time\":400},{\"time\":700}]')}) "
            'ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, profile=EXCLUDED.profile, "updatedAt"=EXCLUDED."updatedAt", disabled=false;'
        )
    # Posts (the content calendar)
    for p in posts():
        parts.append(
            f'INSERT INTO "Post" (id, state, "publishDate", "organizationId", "integrationId", content, delay, "group", "createdAt", "updatedAt", "creationMethod") '
            f"VALUES ({q(p['id'])}, '{p['state']}', {q(p['publish'])}, {q(ORG_ID)}, {q(p['int'])}, {q(content_json(p['text']))}, 0, {q(p['id'] + '-grp')}, {q(now)}, {q(now)}, 'UNKNOWN') "
            'ON CONFLICT (id) DO UPDATE SET state=EXCLUDED.state, "publishDate"=EXCLUDED."publishDate", content=EXCLUDED.content, "integrationId"=EXCLUDED."integrationId", "updatedAt"=EXCLUDED."updatedAt";'
        )

    parts.append("COMMIT;")
    return "\n".join(parts)


def main() -> int:
    # 0. sanity: postgres reachable?
    ping = psql("SELECT 1;")
    if ping.returncode != 0:
        print("Cannot reach Postiz postgres:\n" + ping.stderr, file=sys.stderr)
        return 1

    # 1. hash the password with the container's bcrypt (matches the backend).
    pw_hash = bcrypt_hash(USER_PASSWORD)

    # 2. seed.
    res = psql(build_sql(pw_hash))
    if res.returncode != 0:
        print("Seeding failed:\n" + res.stdout + res.stderr, file=sys.stderr)
        return 1

    # 3. verify + summarise.
    counts = psql(
        'SELECT '
        '(SELECT count(*) FROM "User") , '
        '(SELECT count(*) FROM "Organization"), '
        '(SELECT count(*) FROM "Integration"), '
        "(SELECT count(*) FROM \"Post\" WHERE state IN ('QUEUE','DRAFT')), "
        '(SELECT count(*) FROM "Post");'
    )
    print("Seeded Postiz postgres for Summit Roofing Co.")
    print("  counts (user, org, integrations, scheduled+draft posts, total posts):")
    print("  " + counts.stdout.strip().splitlines()[2].strip())
    print(f"SEED_OK org={ORG_ID} user={USER_EMAIL} integrations={len(INTEGRATIONS)} posts={len(posts())}")

    # 4. persist env for app.py.
    ENV_OUT.write_text(
        f"POSTIZ_FRONT_URL={POSTIZ_FRONT_URL}\n"
        f"POSTIZ_PG_CONTAINER={PG_CONTAINER}\n"
        f"POSTIZ_PG_USER={PG_USER}\n"
        f"POSTIZ_PG_DB={PG_DB}\n"
        f"POSTIZ_ORG_ID={ORG_ID}\n"
        f"POSTIZ_API_KEY={ORG_API_KEY}\n"
    )
    print(f"Wrote {ENV_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
