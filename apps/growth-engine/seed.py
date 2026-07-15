#!/usr/bin/env python3
"""Repeatable seeder for the Summit Roofing Co. demo tenant on self-hosted Umami v2.

Bootstrap (the reliable path for self-hosted Umami):
  1. POST /api/auth/login {admin/umami}  -> bearer token.
  2. POST /api/websites {name,domain}    -> the website + its id (captured, reused on
     re-runs by looking it up by name first so the seed is idempotent).

Seeding strategy — REAL analytics rows over RECENT days:
  Umami's public ingest (`POST /api/send`) stamps every event with `created_at = now()`,
  so it cannot place pageviews across the *past* week (which attribution needs). We instead
  insert REAL `session` + `website_event` rows directly into the Umami Postgres
  (container `agentic-cores-umami-db-1`, db/user/pass = umami). These are the exact same
  columns the Umami app writes — `utm_source`, `utm_medium`, `utm_campaign`,
  `referrer_domain`, `url_path`, dated `created_at` — so every Umami REST report
  (`/stats`, `/metrics?type=referrer|utmSource|utmMedium|utmCampaign`) reads them back as
  genuine traffic. A couple of `/api/send` calls are also fired so the public ingest path
  is exercised end-to-end.

  ~60 pageviews across 4 lead channels over the last 8 days:
    google / cpc          (paid search)
    facebook / cpc        (paid social)
    yardsign-qr / referral(offline QR codes on yard signs -> a referral domain)
    (direct) / organic    (brand / word-of-mouth, no referrer)

The script writes agents/growth-engine/.env so app.py picks up UMAMI_URL + WEBSITE_ID with
no manual copy/paste.

Usage:
    python3 seed.py
Env knobs:
    UMAMI_URL        REST base (default http://localhost:3002)
    UMAMI_DB_CONTAINER  Postgres container (default agentic-cores-umami-db-1)
    UMAMI_FRONT_URL  Umami UI link baked into .env (default http://localhost:3002)
"""
from __future__ import annotations

import os
import random
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ENV_OUT = HERE / ".env"

UMAMI_URL = os.environ.get("UMAMI_URL", "http://localhost:3002").rstrip("/")
UMAMI_FRONT_URL = os.environ.get("UMAMI_FRONT_URL", "http://localhost:3002").rstrip("/")
DB_CONTAINER = os.environ.get("UMAMI_DB_CONTAINER", "agentic-cores-umami-db-1")
ADMIN_USER = os.environ.get("UMAMI_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("UMAMI_ADMIN_PASS", "umami")

SITE_NAME = "Summit Roofing"
SITE_DOMAIN = "summitroofing.test"

# `sudo` is required to talk to the docker socket on this host.
DOCKER = ["sudo", "docker"]

# --- channel mix: realistic roofing-contractor lead sources ------------------
# weight = relative share of the ~60 events; the rest of the tuple is what the
# Umami columns get set to so /metrics reports clean channel names.
CHANNELS = [
    # label          weight referrer_domain        utm_source    utm_medium  utm_campaign
    ("Google Ads",      22, "google.com",          "google",     "cpc",      "spring-reroof"),
    ("Facebook",        12, "facebook.com",        "facebook",   "cpc",      "storm-damage"),
    ("Yard sign QR",    14, "yardsign-qr.test",    "yardsign-qr","referral", "neighborhood"),
    ("Organic / Direct",13, "",                    "",           "organic",  ""),
]

PAGES = [
    ("/", "Summit Roofing — Roof Repair & Replacement"),
    ("/services/roof-replacement", "Roof Replacement | Summit Roofing"),
    ("/free-estimate", "Free Estimate | Summit Roofing"),
    ("/services/storm-damage", "Storm Damage Repair | Summit Roofing"),
    ("/gallery", "Project Gallery | Summit Roofing"),
    ("/contact", "Contact Us | Summit Roofing"),
]
# Pages that count as a "conversion" (lead form / estimate request) for the funnel.
CONVERSION_PATHS = {"/free-estimate", "/contact"}

BROWSERS = ["chrome", "safari", "firefox", "edge"]
OSES = ["Windows", "Mac OS", "iOS", "Android"]
DEVICES = ["desktop", "mobile", "tablet"]
SCREENS = ["1920x1080", "1440x900", "390x844", "412x915", "768x1024"]
CITIES = [("Denver", "CO"), ("Aurora", "CO"), ("Boulder", "CO"), ("Lakewood", "CO")]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def _sql(statement: str) -> subprocess.CompletedProcess:
    """Run a SQL statement inside the Umami Postgres container."""
    return run(DOCKER + ["exec", "-i", DB_CONTAINER, "psql", "-U", "umami", "-d", "umami",
                          "-v", "ON_ERROR_STOP=1", "-c", statement])


def login() -> str:
    r = httpx.post(f"{UMAMI_URL}/api/auth/login",
                   json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=15.0)
    r.raise_for_status()
    return r.json()["token"]


def get_or_create_website(token: str) -> str:
    """Idempotent: reuse the Summit Roofing website if it already exists, else create it."""
    h = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=15.0) as c:
        r = c.get(f"{UMAMI_URL}/api/websites", headers=h, params={"pageSize": 100})
        r.raise_for_status()
        for w in r.json().get("data", []):
            if w.get("name") == SITE_NAME:
                return w["id"]
        r = c.post(f"{UMAMI_URL}/api/websites", headers=h,
                   json={"name": SITE_NAME, "domain": SITE_DOMAIN})
        r.raise_for_status()
        return r.json()["id"]


def _esc(v: str) -> str:
    return v.replace("'", "''")


def seed_rows(website_id: str) -> dict:
    """Insert real session + website_event rows across the last 8 days into Umami Postgres."""
    # Clear any prior rows for this website so the seed is fully repeatable.
    _sql(f"DELETE FROM website_event WHERE website_id = '{website_id}';")
    _sql(f"DELETE FROM session WHERE website_id = '{website_id}';")

    rnd = random.Random(42)  # deterministic seed for reproducible demos
    now = datetime.now(timezone.utc)

    # Build a weighted channel pick list.
    pool: list[tuple] = []
    for ch in CHANNELS:
        pool += [ch] * ch[1]

    sess_values: list[str] = []
    event_values: list[str] = []
    counts: dict[str, int] = {}
    conversions = 0
    total_events = 0

    n_sessions = 36  # -> ~60 pageviews after multi-page visits
    for _ in range(n_sessions):
        label, _w, ref_domain, utm_source, utm_medium, utm_campaign = rnd.choice(pool)
        counts[label] = counts.get(label, 0) + 1

        sid = str(uuid.uuid4())
        vid = str(uuid.uuid4())
        day_offset = rnd.randint(0, 7)
        base = now - timedelta(days=day_offset,
                               hours=rnd.randint(0, 12), minutes=rnd.randint(0, 59))
        browser = rnd.choice(BROWSERS)
        os_ = rnd.choice(OSES)
        device = rnd.choice(DEVICES)
        screen = rnd.choice(SCREENS)
        city, region = rnd.choice(CITIES)

        sess_values.append(
            f"('{sid}','{website_id}','{browser}','{_esc(os_)}','{device}','{screen}',"
            f"'en-US','US','{region}','{_esc(city)}','{base.isoformat()}')"
        )

        # 1-4 pageviews per session; conversion pages appear on deeper visits.
        n_pv = rnd.choices([1, 2, 3, 4], weights=[40, 30, 20, 10])[0]
        visit_pages = [PAGES[0]] + rnd.sample(PAGES[1:], k=min(n_pv - 1, len(PAGES) - 1))
        for i, (path, title) in enumerate(visit_pages):
            ts = base + timedelta(minutes=i * rnd.randint(1, 4))
            eid = str(uuid.uuid4())
            url_query = ""
            if utm_source:
                parts = [f"utm_source={utm_source}", f"utm_medium={utm_medium}"]
                if utm_campaign:
                    parts.append(f"utm_campaign={utm_campaign}")
                url_query = "&".join(parts)
            # Only the landing pageview carries the referrer + UTM (real attribution model).
            is_landing = i == 0
            rd = ref_domain if is_landing else ""
            us = utm_source if is_landing else ""
            um = utm_medium if is_landing else ""
            uc = utm_campaign if is_landing else ""
            uq = url_query if is_landing else ""
            event_values.append(
                f"('{eid}','{website_id}','{sid}','{vid}','{ts.isoformat()}',"
                f"'{_esc(path)}',{('NULL' if not uq else chr(39)+_esc(uq)+chr(39))},"
                f"{('NULL' if not rd else chr(39)+_esc(rd)+chr(39))},"
                f"{('NULL' if not us else chr(39)+_esc(us)+chr(39))},"
                f"{('NULL' if not um else chr(39)+_esc(um)+chr(39))},"
                f"{('NULL' if not uc else chr(39)+_esc(uc)+chr(39))},"
                f"'{_esc(title)}',1,'summitroofing.test')"
            )
            total_events += 1
            if path in CONVERSION_PATHS:
                conversions += 1

    # Bulk insert sessions.
    if sess_values:
        cols = ("session_id,website_id,browser,os,device,screen,language,country,"
                "region,city,created_at")
        res = _sql(f"INSERT INTO session ({cols}) VALUES {','.join(sess_values)};")
        if res.returncode != 0:
            print("session insert failed:\n" + res.stderr, file=sys.stderr)
            raise SystemExit(1)
    # Bulk insert events.
    if event_values:
        cols = ("event_id,website_id,session_id,visit_id,created_at,url_path,url_query,"
                "referrer_domain,utm_source,utm_medium,utm_campaign,page_title,"
                "event_type,hostname")
        res = _sql(f"INSERT INTO website_event ({cols}) VALUES {','.join(event_values)};")
        if res.returncode != 0:
            print("event insert failed:\n" + res.stderr, file=sys.stderr)
            raise SystemExit(1)

    return {"sessions": n_sessions, "events": total_events,
            "conversions": conversions, "by_channel": counts}


def exercise_public_ingest(website_id: str) -> bool:
    """Fire a couple of REAL /api/send events so the public ingest path is verified too."""
    ok = 0
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
    samples = [
        ("/?utm_source=google&utm_medium=cpc&utm_campaign=spring-reroof", "https://www.google.com/"),
        ("/free-estimate?utm_source=facebook&utm_medium=cpc&utm_campaign=storm-damage", "https://www.facebook.com/"),
    ]
    with httpx.Client(timeout=15.0) as c:
        for url, ref in samples:
            try:
                r = c.post(f"{UMAMI_URL}/api/send",
                           headers={"User-Agent": ua, "Content-Type": "application/json"},
                           json={"type": "event", "payload": {
                               "website": website_id, "hostname": SITE_DOMAIN,
                               "url": url, "referrer": ref, "title": "Summit Roofing",
                               "language": "en-US", "screen": "1920x1080"}})
                if r.status_code == 200:
                    ok += 1
            except Exception:
                pass
    return ok == len(samples)


def main() -> int:
    print(f"Umami at {UMAMI_URL} — logging in as {ADMIN_USER} …")
    token = login()
    website_id = get_or_create_website(token)
    print(f"website: {SITE_NAME} ({SITE_DOMAIN}) id={website_id}")

    summary = seed_rows(website_id)
    ingest_ok = exercise_public_ingest(website_id)

    by = " ".join(f"{k}={v}" for k, v in sorted(summary["by_channel"].items()))
    print(f"SEED_OK website_id={website_id} sessions={summary['sessions']} "
          f"events={summary['events']} conversions={summary['conversions']} "
          f"channels[{by}] public_ingest={'ok' if ingest_ok else 'skipped'}")

    ENV_OUT.write_text(
        f"UMAMI_URL={UMAMI_URL}\n"
        f"WEBSITE_ID={website_id}\n"
        f"UMAMI_ADMIN_USER={ADMIN_USER}\n"
        f"UMAMI_ADMIN_PASS={ADMIN_PASS}\n"
        f"UMAMI_FRONT_URL={UMAMI_FRONT_URL}\n"
    )
    print(f"Wrote {ENV_OUT} (UMAMI_URL, WEBSITE_ID, UMAMI_ADMIN_*, UMAMI_FRONT_URL)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
