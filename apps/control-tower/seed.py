#!/usr/bin/env python3
"""Register the Summit Roofing Postgres datasource in Metabase, sync it, and create the saved
cards + dashboard the owner cares about — idempotent, stdlib-only.

The operational data lives in control-tower-db (Postgres, seeded from roofing.sql). This script
teaches Metabase about it and builds real, native-SQL saved questions so both the agent (via
/api/dataset) and the Metabase UI ("Open in Metabase") show the same live numbers.

    METABASE_API_URL=http://localhost:3001 \
    METABASE_ADMIN_EMAIL=... METABASE_ADMIN_PASSWORD=... \
    python3 seed.py

Env:
  METABASE_API_URL         REST base (default http://localhost:3001)
  METABASE_SESSION         reuse an existing X-Metabase-Session token (else log in / set up)
  METABASE_ADMIN_EMAIL / METABASE_ADMIN_PASSWORD   admin creds for login/first-run setup
  PGHOST / PGPORT / PGDB / PGUSER / PGPASS         how Metabase reaches the Postgres (defaults below)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("METABASE_API_URL", "http://localhost:3001").rstrip("/")
ADMIN_EMAIL = os.environ.get("METABASE_ADMIN_EMAIL", "admin@summitroofing.test")
ADMIN_PASSWORD = os.environ.get("METABASE_ADMIN_PASSWORD", "SummitRoof!2026")
DB_NAME = "Summit Roofing"
PG = {
    "host": os.environ.get("PGHOST", "control-tower-db"),
    "port": int(os.environ.get("PGPORT", "5432")),
    "dbname": os.environ.get("PGDB", "roofing"),
    "user": os.environ.get("PGUSER", "summit"),
    "password": os.environ.get("PGPASS", "summit"),
    "ssl": False,
}


def api(method: str, path: str, token: str | None = None, body: dict | None = None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Metabase-Session", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw}


def get_session() -> str:
    if os.environ.get("METABASE_SESSION"):
        return os.environ["METABASE_SESSION"]
    st, body = api("POST", "/api/session", body={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    if st == 200 and body.get("id"):
        return body["id"]
    st, props = api("GET", "/api/session/properties")
    token = (props or {}).get("setup-token")
    if not token:
        sys.exit(f"cannot log in and no setup-token available (status {st}); set METABASE_SESSION")
    st, body = api("POST", "/api/setup", body={
        "token": token,
        "user": {"first_name": "Summit", "last_name": "Admin", "email": ADMIN_EMAIL,
                 "password": ADMIN_PASSWORD, "site_name": "Summit Roofing Co."},
        "prefs": {"site_name": "Summit Roofing Co.", "allow_tracking": False},
    })
    if st in (200, 201) and body.get("id"):
        return body["id"]
    sys.exit(f"setup failed: {st} {body}")


def ensure_database(token: str) -> int:
    st, body = api("GET", "/api/database", token)
    dbs = body.get("data", body) if isinstance(body, dict) else body
    for d in (dbs or []):
        if d.get("name") == DB_NAME:
            print(f"datasource '{DB_NAME}' already registered -> id {d['id']}")
            return d["id"]
    st, body = api("POST", "/api/database", token, {
        "name": DB_NAME, "engine": "postgres", "details": PG, "is_full_sync": True,
    })
    if st not in (200, 201) or not body.get("id"):
        sys.exit(f"failed to register datasource: {st} {body}")
    db_id = body["id"]
    print(f"registered datasource '{DB_NAME}' -> id {db_id}")
    return db_id


def wait_for_tables(token: str, db_id: int, want=("jobs", "customers", "invoices"), tries=30):
    api("POST", f"/api/database/{db_id}/sync_schema", token)
    for _ in range(tries):
        st, meta = api("GET", f"/api/database/{db_id}/metadata", token)
        tables = {t["name"] for t in (meta.get("tables") or [])}
        if set(want) <= tables:
            print(f"schema synced: {sorted(tables & set(want))}")
            return True
        time.sleep(2)
    print("WARNING: tables not visible yet after sync; agent /api/dataset queries still work once synced")
    return False


# The saved questions an owner actually asks — REAL native Postgres SQL.
CARDS = [
    ("Revenue by month", "line",
     "SELECT to_char(completed_date,'YYYY-MM') AS month, round(sum(invoiced_amount)) AS revenue "
     "FROM jobs WHERE status='Completed' AND completed_date >= (CURRENT_DATE - INTERVAL '18 months') "
     "GROUP BY 1 ORDER BY 1"),
    ("Revenue by service line", "bar",
     "SELECT service_type, count(*) AS jobs, round(sum(invoiced_amount)) AS revenue "
     "FROM jobs WHERE status='Completed' GROUP BY 1 ORDER BY revenue DESC"),
    ("Gross margin by service line", "bar",
     "SELECT service_type, round(sum(invoiced_amount)) AS revenue, "
     "round(100*(sum(invoiced_amount)-sum(material_cost)-sum(labor_cost))/nullif(sum(invoiced_amount),0)) AS margin_pct "
     "FROM jobs WHERE status='Completed' GROUP BY 1 ORDER BY margin_pct DESC"),
    ("Revenue & win-rate by lead source", "table",
     "SELECT lead_source, count(*) FILTER (WHERE status!='Lost') AS won, count(*) AS quotes, "
     "round(100.0*count(*) FILTER (WHERE status!='Lost')/count(*)) AS win_pct, "
     "round(sum(invoiced_amount)) AS revenue FROM jobs GROUP BY 1 ORDER BY revenue DESC"),
    ("Quote-to-job conversion by month", "line",
     "SELECT to_char(quote_date,'YYYY-MM') AS month, "
     "round(100.0*count(*) FILTER (WHERE status!='Lost')/count(*)) AS conversion_pct "
     "FROM jobs WHERE quote_date >= (CURRENT_DATE - INTERVAL '18 months') GROUP BY 1 ORDER BY 1"),
    ("Accounts receivable aging", "table",
     "SELECT CASE WHEN CURRENT_DATE<=due_date THEN '0 current' "
     "WHEN CURRENT_DATE-due_date<=30 THEN '1-30' WHEN CURRENT_DATE-due_date<=60 THEN '31-60' "
     "WHEN CURRENT_DATE-due_date<=90 THEN '61-90' ELSE '90+' END AS bucket, "
     "count(*) AS invoices, round(sum(amount)) AS outstanding "
     "FROM invoices WHERE status='Open' GROUP BY 1 ORDER BY 1"),
]


def create_cards_and_dashboard(token: str, db_id: int):
    card_ids = []
    for name, display, sql in CARDS:
        st, body = api("POST", "/api/card", token, {
            "name": name, "display": display, "visualization_settings": {},
            "dataset_query": {"type": "native", "native": {"query": sql}, "database": db_id},
        })
        if st in (200, 201) and body.get("id"):
            card_ids.append((body["id"], name))
            print(f"  card ok {name} (id {body['id']})")
        else:
            print(f"  card FAIL {name}: {st} {str(body)[:140]}")
    if not card_ids:
        return None
    st, dash = api("POST", "/api/dashboard", token, {"name": "Summit Roofing - Control Tower"})
    if st not in (200, 201) or not dash.get("id"):
        print(f"  dashboard create failed: {st} {dash}")
        return None
    dash_id = dash["id"]
    dashcards = [{"id": -(i + 1), "card_id": cid, "row": (i // 2) * 4, "col": (i % 2) * 12,
                  "size_x": 12, "size_y": 4} for i, (cid, _n) in enumerate(card_ids)]
    st, _ = api("PUT", f"/api/dashboard/{dash_id}", token, {"dashcards": dashcards})
    if st not in (200, 201):
        for i, (cid, _n) in enumerate(card_ids):
            api("POST", f"/api/dashboard/{dash_id}/cards", token,
                {"cardId": cid, "row": (i // 2) * 4, "col": (i % 2) * 12, "size_x": 12, "size_y": 4})
    print(f"dashboard ok id {dash_id} with {len(card_ids)} cards -> {API}/dashboard/{dash_id}")
    return dash_id


def main() -> int:
    token = get_session()
    db_id = ensure_database(token)
    wait_for_tables(token, db_id)
    create_cards_and_dashboard(token, db_id)
    print(f"\nDONE. METABASE_DB_ID={db_id}  (agent resolves '{DB_NAME}' by name via METABASE_DB_NAME)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
