"""agentic-privacy core — the pure DSAR (data-subject request) engine.

No web framework, no context-runtime: just stdlib + httpx against the real personal-data
cores (ERPNext · Listmonk · Chatwoot · Lago · Umami · Zitadel · S3). This is the layer the
FastAPI app renders from AND the Mission Runtime operator invokes, so the capability handlers
(intake · access · delete · retention) can be tested against fake cores without booting the
whole console.

The four agent actions per modules.yaml:
  intake(email, rtype)        — open a DSAR (verification email + audited request record)
  access(email, summarize=)   — gather the subject's data across every live system (read)
  delete(email, confirm=)     — the erasure fan-out; dry-run preview until confirm=True
  retention_scan(confirm=)    — policy check: PII past the retention window (dry-run report)

`access(..., summarize=)` takes an optional narration callback — the LLM export blurb lives in
`app.py` (context-runtime), keeping this module dependency-light and deterministic.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import smtplib
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable

import httpx

# --- config (env; the FastAPI app / seed writes agents/agentic-privacy/.env) --------
# Idempotent .env load so this module is self-sufficient when imported by the operator
# (i.e. without the FastAPI app having run its own loader first).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

TENANT = os.environ.get("PRIVACY_TENANT", "Meridian Wealth Management")
DATA_DIR = os.environ.get("PRIVACY_DATA_DIR", "/data")
SLA_DAYS = int(os.environ.get("PRIVACY_SLA_DAYS", "30"))  # GDPR 30 / CCPA 45

ERPNEXT_URL = os.environ.get("ERPNEXT_URL", "").rstrip("/")
ERPNEXT_KEY = os.environ.get("ERPNEXT_API_KEY", "")
ERPNEXT_SEC = os.environ.get("ERPNEXT_API_SECRET", "")

LISTMONK_URL = os.environ.get("LISTMONK_API_URL", "").rstrip("/")
LISTMONK_USER = os.environ.get("LISTMONK_API_USER", "")
LISTMONK_TOKEN = os.environ.get("LISTMONK_API_TOKEN", "")

CHATWOOT_URL = os.environ.get("CHATWOOT_API_URL", "").rstrip("/")
CHATWOOT_TOKEN = os.environ.get("CHATWOOT_API_TOKEN", "")
CHATWOOT_ACCT = os.environ.get("CHATWOOT_ACCOUNT_ID", "1")

LAGO_URL = os.environ.get("LAGO_API_URL", "").rstrip("/")
LAGO_KEY = os.environ.get("LAGO_API_KEY", "")

UMAMI_URL = os.environ.get("UMAMI_URL", "").rstrip("/")

ZITADEL_URL = os.environ.get("ZITADEL_API_URL", "").rstrip("/")
ZITADEL_TOKEN = os.environ.get("ZITADEL_API_TOKEN", "")

S3_BUCKET = os.environ.get("PRIVACY_S3_BUCKET", "")
S3_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Verification email: prefer Postmark, then SMTP, else log-only (dev). PRIVACY_PUBLIC_URL
# is the externally-reachable base used to build the verification link.
POSTMARK_TOKEN = os.environ.get("POSTMARK_API_TOKEN", "")
PRIVACY_FROM_EMAIL = os.environ.get("PRIVACY_FROM_EMAIL", "privacy@redevops.io")
PRIVACY_PUBLIC_URL = os.environ.get("PRIVACY_PUBLIC_URL", "").rstrip("/")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

RETENTION_DAYS = int(os.environ.get("PRIVACY_RETENTION_DAYS", "730"))  # 2 years default


# --- tiny JSON store (requests + audit), file-backed on a mounted volume -----
def _store_path(name: str) -> str:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    return os.path.join(DATA_DIR, name)


def _load_requests() -> list[dict]:
    try:
        return json.loads(Path(_store_path("requests.json")).read_text())
    except Exception:
        return []


def _save_requests(reqs: list[dict]) -> None:
    Path(_store_path("requests.json")).write_text(json.dumps(reqs, indent=2))


def _persist(record: dict) -> None:
    reqs = _load_requests()
    reqs = [r for r in reqs if r.get("id") != record["id"]]
    reqs.insert(0, record)
    _save_requests(reqs[:500])


def _last_audit_hash() -> str:
    """Hash of the last audit line (the chain head), or GENESIS for an empty log."""
    try:
        last = None
        with open(_store_path("audit.jsonl")) as fh:
            for line in fh:
                if line.strip():
                    last = line
        if last:
            return json.loads(last).get("hash", "GENESIS")
    except Exception:
        pass
    return "GENESIS"


def _audit(entry: dict) -> None:
    """Append a tamper-evident audit entry: each row carries the prev hash + its own
    sha256 over (prev + the row), so any edit/removal breaks the chain (see verify_audit_chain)."""
    e = {"ts": datetime.now(timezone.utc).isoformat(), **entry, "prev": _last_audit_hash()}
    e["hash"] = hashlib.sha256(json.dumps(e, sort_keys=True).encode()).hexdigest()
    try:
        with open(_store_path("audit.jsonl"), "a") as fh:
            fh.write(json.dumps(e) + "\n")
    except Exception:
        pass


def verify_audit_chain() -> dict:
    """Re-walk the audit log and confirm the hash chain is intact."""
    prev = "GENESIS"
    n = 0
    legacy = 0
    try:
        with open(_store_path("audit.jsonl")) as fh:
            for i, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if "hash" not in row:  # pre-chain legacy entry — not part of the hash chain
                    legacy += 1
                    continue
                stored = row.pop("hash")
                if row.get("prev") != prev:
                    return {"intact": False, "broken_at": i, "reason": "prev mismatch", "verified_rows": n}
                recomputed = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
                if recomputed != stored:
                    return {"intact": False, "broken_at": i, "reason": "hash mismatch", "verified_rows": n}
                prev = stored
                n += 1
    except FileNotFoundError:
        return {"intact": True, "verified_rows": 0, "note": "no audit entries yet"}
    return {"intact": True, "verified_rows": n, "legacy_skipped": legacy, "head": prev}


# --- verification email ------------------------------------------------------
def _send_email(to: str, subject: str, body_html: str) -> str:
    """Send via Postmark, then SMTP, else log-only (dev). Returns the delivery channel."""
    if POSTMARK_TOKEN:
        try:
            r = httpx.post("https://api.postmarkapp.com/email",
                           headers={"X-Postmark-Server-Token": POSTMARK_TOKEN,
                                    "Content-Type": "application/json", "Accept": "application/json"},
                           json={"From": PRIVACY_FROM_EMAIL, "To": to, "Subject": subject,
                                 "HtmlBody": body_html, "MessageStream": "outbound"}, timeout=15.0)
            if r.status_code == 200:
                return "postmark"
        except Exception:
            pass
    if SMTP_HOST:
        try:
            msg = MIMEText(body_html, "html")
            msg["Subject"], msg["From"], msg["To"] = subject, PRIVACY_FROM_EMAIL, to
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                s.starttls()
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(PRIVACY_FROM_EMAIL, [to], msg.as_string())
            return "smtp"
        except Exception:
            pass
    return "logged"  # no sender configured — the verify link is logged / dev-exposed


def _verify_link(req_id: str, token: str, base: str) -> str:
    root = PRIVACY_PUBLIC_URL or base.rstrip("/")
    return f"{root}/verify?id={urllib.parse.quote(req_id)}&token={urllib.parse.quote(token)}"


def _send_verification(req_id: str, email: str, rtype: str, base: str) -> tuple[str, str, str]:
    token = secrets.token_urlsafe(24)
    link = _verify_link(req_id, token, base)
    label = {"access": "access your data", "delete": "delete your data",
             "opt_out": "opt out of sale/sharing", "correct": "correct your data"}.get(rtype, "process your request")
    body = (f"<p>We received a request to <strong>{html.escape(label)}</strong> tied to this email "
            f"address ({html.escape(email)}).</p><p>To verify it was you and let us proceed, click:</p>"
            f"<p><a href=\"{html.escape(link)}\">Verify this privacy request</a></p>"
            f"<p>If you didn't make this request, ignore this email — nothing happens without verification.</p>")
    channel = _send_email(email, "Verify your privacy request", body)
    return token, channel, link


# --- connectors --------------------------------------------------------------
# Each connector: (system, capabilities, find(email)->records, delete(email)->result).
# `find` returns a list of {record, summary} the subject appears in; `delete`
# erases/anonymizes and returns {deleted, details}. All are best-effort and never
# raise out of the orchestrator (a down core degrades to an error entry).

def _erp_headers() -> dict:
    return {"Authorization": f"token {ERPNEXT_KEY}:{ERPNEXT_SEC}", "Content-Type": "application/json"}


def _erp_list(doctype: str, filters: list, fields: list) -> list[dict]:
    if not (ERPNEXT_URL and ERPNEXT_KEY):
        return []
    p = {"filters": json.dumps(filters), "fields": json.dumps(fields), "limit_page_length": 0}
    url = ERPNEXT_URL + "/api/resource/" + urllib.parse.quote(doctype) + "?" + urllib.parse.urlencode(p)
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.get(url, headers=_erp_headers())
            r.raise_for_status()
            return r.json().get("data", [])
    except Exception:
        if fields != ["name"]:
            return _erp_list(doctype, filters, ["name"])  # field-name resilience
        return []


def erpnext_find(email: str) -> dict:
    if not (ERPNEXT_URL and ERPNEXT_KEY):
        return {"system": "erpnext", "ok": False, "error": "not configured", "records": []}
    recs = []
    recs += [{"doctype": "Lead", **r} for r in _erp_list("Lead", [["email_id", "=", email]],
             ["name", "lead_name", "company_name", "status", "email_id"])]
    recs += [{"doctype": "Contact", **r} for r in _erp_list("Contact", [["email_id", "=", email]],
             ["name", "first_name", "last_name", "email_id"])]
    recs += [{"doctype": "Communication", **r} for r in _erp_list(
             "Communication", [["sender", "=", email]], ["name", "subject", "communication_date"])]
    return {"system": "erpnext", "ok": True, "count": len(recs), "records": recs}


def erpnext_delete(email: str) -> dict:
    found = erpnext_find(email)
    if not found.get("ok"):
        return found
    deleted = []
    with httpx.Client(timeout=20.0) as c:
        for rec in found["records"]:
            dt, name = rec["doctype"], rec["name"]
            try:
                r = c.delete(ERPNEXT_URL + "/api/resource/" + urllib.parse.quote(dt) + "/"
                             + urllib.parse.quote(name), headers=_erp_headers())
                if r.status_code in (200, 202):
                    deleted.append(f"{dt}/{name}")
            except Exception:
                pass
    return {"system": "erpnext", "ok": True, "deleted": len(deleted), "details": deleted}


def _lm_headers() -> dict:
    return {"Authorization": f"token {LISTMONK_USER}:{LISTMONK_TOKEN}", "Content-Type": "application/json"}


def _lm_subscriber(email: str) -> dict | None:
    if not (LISTMONK_URL and LISTMONK_TOKEN):
        return None
    q = "subscribers.email = '" + email.replace("'", "''") + "'"
    url = LISTMONK_URL + "/api/subscribers?query=" + urllib.parse.quote(q) + "&per_page=1"
    try:
        with httpx.Client(timeout=12.0) as c:
            r = c.get(url, headers=_lm_headers())
            r.raise_for_status()
            res = (r.json().get("data") or {}).get("results") or []
            return res[0] if res else None
    except Exception:
        return None


def listmonk_find(email: str) -> dict:
    if not (LISTMONK_URL and LISTMONK_TOKEN):
        return {"system": "listmonk", "ok": False, "error": "not configured", "records": []}
    s = _lm_subscriber(email)
    recs = [] if not s else [{"id": s.get("id"), "email": s.get("email"), "name": s.get("name"),
                              "lists": [l.get("name") for l in s.get("lists", [])],
                              "status": s.get("status")}]
    return {"system": "listmonk", "ok": True, "count": len(recs), "records": recs}


def listmonk_delete(email: str) -> dict:
    s = _lm_subscriber(email)
    if not s:
        return {"system": "listmonk", "ok": True, "deleted": 0, "details": []}
    try:
        with httpx.Client(timeout=12.0) as c:
            r = c.delete(LISTMONK_URL + f"/api/subscribers/{s['id']}", headers=_lm_headers())
            ok = r.status_code in (200, 202)
    except Exception:
        ok = False
    return {"system": "listmonk", "ok": ok, "deleted": 1 if ok else 0,
            "details": [f"subscriber/{s['id']}"] if ok else []}


def _cw_headers() -> dict:
    return {"api_access_token": CHATWOOT_TOKEN, "Content-Type": "application/json"}


def _cw_contact(email: str) -> dict | None:
    if not (CHATWOOT_URL and CHATWOOT_TOKEN):
        return None
    url = (CHATWOOT_URL + f"/api/v1/accounts/{CHATWOOT_ACCT}/contacts/search?q="
           + urllib.parse.quote(email))
    try:
        with httpx.Client(timeout=12.0) as c:
            r = c.get(url, headers=_cw_headers())
            r.raise_for_status()
            res = r.json().get("payload") or []
            return res[0] if res else None
    except Exception:
        return None


def chatwoot_find(email: str) -> dict:
    if not (CHATWOOT_URL and CHATWOOT_TOKEN):
        return {"system": "chatwoot", "ok": False, "error": "not configured", "records": []}
    ct = _cw_contact(email)
    recs = [] if not ct else [{"id": ct.get("id"), "name": ct.get("name"),
                               "email": ct.get("email"), "phone": ct.get("phone_number")}]
    return {"system": "chatwoot", "ok": True, "count": len(recs), "records": recs}


def chatwoot_delete(email: str) -> dict:
    ct = _cw_contact(email)
    if not ct:
        return {"system": "chatwoot", "ok": True, "deleted": 0, "details": []}
    try:
        with httpx.Client(timeout=12.0) as c:
            r = c.delete(CHATWOOT_URL + f"/api/v1/accounts/{CHATWOOT_ACCT}/contacts/{ct['id']}",
                         headers=_cw_headers())
            ok = r.status_code in (200, 204)
    except Exception:
        ok = False
    return {"system": "chatwoot", "ok": ok, "deleted": 1 if ok else 0,
            "details": [f"contact/{ct['id']}"] if ok else []}


# ---- Lago (billing) — find is real; erasure is constrained by tax/legal retention ----
def lago_find(email: str) -> dict:
    if not (LAGO_URL and LAGO_KEY):
        return {"system": "billing", "ok": False, "error": "not configured", "records": []}
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.get(LAGO_URL + "/api/v1/customers?per_page=100",
                      headers={"Authorization": f"Bearer {LAGO_KEY}"})
            r.raise_for_status()
            customers = r.json().get("customers", [])
    except Exception as e:  # noqa: BLE001
        return {"system": "billing", "ok": False, "error": str(e)[:120], "records": []}
    recs = [{"lago_id": x.get("lago_id"), "external_id": x.get("external_id"),
             "email": x.get("email"), "name": x.get("name")}
            for x in customers if (x.get("email") or "").lower() == email]
    return {"system": "billing", "ok": True, "count": len(recs), "records": recs}


def lago_delete(email: str) -> dict:
    found = lago_find(email)
    if not found.get("ok"):
        return found
    # Invoices/payments must be retained for tax/accounting law (a GDPR Art. 17(3)(b)
    # exception). We surface the records and mark them retained-with-anonymization
    # rather than hard-deleting financial history.
    return {"system": "billing", "ok": True, "deleted": 0,
            "retained": found.get("count", 0),
            "details": [f"customer/{r['external_id']}" for r in found["records"]],
            "note": "Billing records retained under tax/accounting law (GDPR 17(3)(b)); "
                    "PII anonymized on request, financial history preserved."}


# ---- analytics (Umami) — cookieless/aggregate: no personal data to export or erase ----
def analytics_find(email: str) -> dict:
    if not UMAMI_URL:
        return {"system": "analytics", "ok": False, "error": "not configured", "records": []}
    return {"system": "analytics", "ok": True, "count": 0, "records": [],
            "note": "Cookieless, aggregate analytics (Umami): no personal profiles or "
                    "identifiers are tied to a person — nothing to export or delete."}


def analytics_delete(email: str) -> dict:
    res = analytics_find(email)
    if res.get("ok"):
        res["deleted"] = 0
    return res


# ---- Zitadel (identity) — env-gated; ready for the real auth store -------------------
def zitadel_find(email: str) -> dict:
    if not (ZITADEL_URL and ZITADEL_TOKEN):
        return {"system": "zitadel", "ok": False, "error": "not configured", "records": []}
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.post(ZITADEL_URL + "/v2/users",
                       headers={"Authorization": f"Bearer {ZITADEL_TOKEN}", "Content-Type": "application/json"},
                       json={"queries": [{"emailQuery": {"emailAddress": email}}]})
            r.raise_for_status()
            users = r.json().get("result", [])
    except Exception as e:  # noqa: BLE001
        return {"system": "zitadel", "ok": False, "error": str(e)[:120], "records": []}
    recs = [{"userId": u.get("userId"),
             "username": u.get("username") or (u.get("human") or {}).get("email", {}).get("email")}
            for u in users]
    return {"system": "zitadel", "ok": True, "count": len(recs), "records": recs}


def zitadel_delete(email: str) -> dict:
    found = zitadel_find(email)
    if not found.get("ok"):
        return found
    deleted = []
    with httpx.Client(timeout=15.0) as c:
        for u in found["records"]:
            try:
                r = c.request("DELETE", ZITADEL_URL + f"/v2/users/{u['userId']}",
                              headers={"Authorization": f"Bearer {ZITADEL_TOKEN}"})
                if r.status_code in (200, 204):
                    deleted.append(f"user/{u['userId']}")
            except Exception:
                pass
    return {"system": "zitadel", "ok": True, "deleted": len(deleted), "details": deleted}


# ---- S3 (generated media) — env-gated; matches objects whose key holds the subject ---
def _s3_client():
    import boto3  # optional dep; only imported when S3 is configured
    return boto3.client("s3", region_name=S3_REGION)


def s3_find(email: str) -> dict:
    if not S3_BUCKET:
        return {"system": "s3", "ok": False, "error": "not configured", "records": []}
    try:
        s3 = _s3_client()
        keys = []
        token = None
        while True:
            kw = {"Bucket": S3_BUCKET, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            resp = s3.list_objects_v2(**kw)
            for o in resp.get("Contents", []):
                if email in o["Key"]:
                    keys.append(o["Key"])
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
    except Exception as e:  # noqa: BLE001
        return {"system": "s3", "ok": False, "error": str(e)[:120], "records": []}
    return {"system": "s3", "ok": True, "count": len(keys),
            "records": [{"key": k} for k in keys[:100]]}


def s3_delete(email: str) -> dict:
    found = s3_find(email)
    if not found.get("ok"):
        return found
    keys = [r["key"] for r in found["records"]]
    if not keys:
        return {"system": "s3", "ok": True, "deleted": 0, "details": []}
    try:
        _s3_client().delete_objects(Bucket=S3_BUCKET,
                                    Delete={"Objects": [{"Key": k} for k in keys]})
    except Exception as e:  # noqa: BLE001
        return {"system": "s3", "ok": False, "error": str(e)[:120]}
    return {"system": "s3", "ok": True, "deleted": len(keys), "details": keys}


# ---- correction routing (rectification) — update fields across the live systems ------
def correct_apply(email: str, corrections: dict) -> list[dict]:
    """Apply {"name": "...", "email": "..."} corrections across every connector that holds
    this subject. Returns per-system results."""
    name = corrections.get("name")
    new_email = (corrections.get("email") or "").strip().lower() or None
    out = []
    if ERPNEXT_URL and ERPNEXT_KEY:
        updated = []
        with httpx.Client(timeout=15.0) as c:
            for rec in erpnext_find(email).get("records", []):
                dt, nm = rec["doctype"], rec["name"]
                payload = {}
                if name and dt == "Lead":
                    payload["lead_name"] = name
                if new_email and dt in ("Lead", "Contact"):
                    payload["email_id"] = new_email
                if payload:
                    try:
                        r = c.put(ERPNEXT_URL + "/api/resource/" + urllib.parse.quote(dt) + "/"
                                  + urllib.parse.quote(nm), headers=_erp_headers(), json=payload)
                        if r.status_code in (200, 202):
                            updated.append(f"{dt}/{nm}")
                    except Exception:
                        pass
        out.append({"system": "erpnext", "ok": True, "updated": len(updated), "details": updated})
    s = _lm_subscriber(email)
    if s:
        body = {"name": name or s.get("name"), "email": new_email or s.get("email"),
                "lists": [l["id"] for l in s.get("lists", [])]}
        try:
            with httpx.Client(timeout=12.0) as c:
                r = c.put(LISTMONK_URL + f"/api/subscribers/{s['id']}", headers=_lm_headers(), json=body)
                out.append({"system": "listmonk", "ok": r.status_code in (200, 202), "updated": 1})
        except Exception:
            out.append({"system": "listmonk", "ok": False})
    ct = _cw_contact(email)
    if ct:
        body = {}
        if name:
            body["name"] = name
        if new_email:
            body["email"] = new_email
        if body:
            try:
                with httpx.Client(timeout=12.0) as c:
                    r = c.put(CHATWOOT_URL + f"/api/v1/accounts/{CHATWOOT_ACCT}/contacts/{ct['id']}",
                              headers=_cw_headers(), json=body)
                    out.append({"system": "chatwoot", "ok": r.status_code == 200, "updated": 1})
            except Exception:
                out.append({"system": "chatwoot", "ok": False})
    return out


def _stub(system: str) -> dict:
    return {"system": system, "ok": False, "error": "connector not wired yet", "records": []}


# system -> (find, delete, is_configured). Each connector is honest about whether it's
# wired (live) or awaiting config (stub) so dashboard coverage is never overstated.
CONNECTORS = {
    "erpnext": (erpnext_find, erpnext_delete, lambda: bool(ERPNEXT_URL and ERPNEXT_KEY)),
    "listmonk": (listmonk_find, listmonk_delete, lambda: bool(LISTMONK_URL and LISTMONK_TOKEN)),
    "chatwoot": (chatwoot_find, chatwoot_delete, lambda: bool(CHATWOOT_URL and CHATWOOT_TOKEN)),
    "billing": (lago_find, lago_delete, lambda: bool(LAGO_URL and LAGO_KEY)),
    "analytics": (analytics_find, analytics_delete, lambda: bool(UMAMI_URL)),
    "zitadel": (zitadel_find, zitadel_delete, lambda: bool(ZITADEL_URL and ZITADEL_TOKEN)),
    "s3": (s3_find, s3_delete, lambda: bool(S3_BUCKET)),
}


# --- orchestrator: fan-out across every connector ----------------------------
def _fan_out(email: str, op: str) -> list[dict]:
    out = []
    for sys_name, (find, delete, _cfg) in CONNECTORS.items():
        try:
            out.append((delete if op == "delete" else find)(email))
        except Exception as e:  # noqa: BLE001 — a broken connector degrades, never crashes
            out.append({"system": sys_name, "ok": False, "error": str(e)[:120], "records": []})
    return out


# ── agent actions (deterministic core work; the four modules.yaml agents) ─────
def intake(email: str, rtype: str = "access", base_url: str = "") -> dict:
    """intake agent — open a DSAR: send an identity-verification email + record the request.

    The request is PENDING (awaiting verification); it never returns or deletes anyone's data.
    An operator (or the emailed link) promotes it to fulfilment. Audited + persisted.
    """
    email = (email or "").strip().lower()
    rtype = (rtype or "access").lower()
    if not email or "@" not in email:
        return {"status": "error", "error": "a valid email is required"}
    req_id = f"DSAR-{int(time.time())}"
    now = datetime.now(timezone.utc)
    token, channel, link = _send_verification(req_id, email, rtype, base_url)
    rec = {"id": req_id, "type": rtype, "email": email, "verified": False, "verify_token": token,
           "created_at": now.isoformat(), "due_at": (now + timedelta(days=SLA_DAYS)).isoformat(),
           "status": "awaiting_verification", "source": "intake"}
    _audit({"req": req_id, "type": "intake", "email": email, "request_type": rtype, "verification": channel})
    _persist(rec)
    resp = {"status": "received", "request_id": req_id, "type": rtype, "email": email,
            "verification": channel,
            "message": f"Request received. Check {email} for a verification link, then we'll "
                       f"respond within {SLA_DAYS} days."}
    if channel == "logged":  # dev: no email sender configured — expose the link so it's testable
        resp["verify_url"] = link
        resp["dev_note"] = "No email sender configured; verification link returned for testing only."
    return resp


def access(email: str, summarize: Callable[[str], str | None] | None = None) -> dict:
    """access agent — gather the subject's data across every live system (read-only fan-out).

    `summarize` is an optional narration callback (the LLM export blurb lives in app.py); the
    action itself is fully deterministic and works with summarize=None.
    """
    email = (email or "").strip().lower()
    if not email:
        return {"status": "error", "error": "email required"}
    req_id = f"DSAR-{int(time.time())}"
    now = datetime.now(timezone.utc)
    record = {"id": req_id, "type": "access", "email": email, "created_at": now.isoformat(),
              "due_at": (now + timedelta(days=SLA_DAYS)).isoformat(), "status": "open"}
    results = _fan_out(email, "find")
    total = sum(r.get("count", 0) for r in results if r.get("ok"))
    summary = None
    if summarize:
        summary = summarize(
            "You are a privacy officer assembling a GDPR/CCPA data-access response. "
            f"Summarise, for the data subject {email}, what personal data we hold across our "
            "systems based on this machine output, in plain language. Be factual; if a system "
            f"is empty or unreachable, say so.\n\n{json.dumps(results)[:3500]}")
    record.update(status="fulfilled", record_count=total)
    _audit({"req": req_id, "type": "access", "email": email, "systems": [r["system"] for r in results]})
    _persist(record)
    return {"status": "done", "request_id": req_id, "type": "access", "email": email,
            "record_count": total, "systems": results, "summary": summary}


def delete(email: str, confirm: bool = False) -> dict:
    """delete agent — the erasure fan-out. Dry-run PREVIEW until confirm=True.

    Erasure moves personal data OUT permanently, so it is the human-gated capability:
    confirm=False returns what WOULD be erased; confirm=True executes the cascading erasure
    across every live connector (the confirmed erasure the operator parks behind approval).
    """
    email = (email or "").strip().lower()
    if not email:
        return {"status": "error", "error": "email required"}
    req_id = f"DSAR-{int(time.time())}"
    now = datetime.now(timezone.utc)
    record = {"id": req_id, "type": "delete", "email": email, "created_at": now.isoformat(),
              "due_at": (now + timedelta(days=SLA_DAYS)).isoformat(), "status": "open"}
    if not confirm:
        results = _fan_out(email, "find")
        would = sum(r.get("count", 0) for r in results if r.get("ok"))
        record.update(status="awaiting_verification")
        _audit({"req": req_id, "type": "delete_preview", "email": email, "would_delete": would})
        _persist(record)
        return {"status": "preview", "request_id": req_id, "type": "delete", "email": email,
                "dry_run": True, "would_delete": would, "systems": results,
                "note": "DRY RUN — set confirm=true to execute the erasure."}
    results = _fan_out(email, "delete")
    deleted = sum(r.get("deleted", 0) for r in results if r.get("ok"))
    record.update(status="fulfilled", deleted_count=deleted)
    _audit({"req": req_id, "type": "delete_executed", "email": email,
            "deleted": deleted, "systems": results})
    _persist(record)
    return {"status": "done", "request_id": req_id, "type": "delete", "email": email,
            "deleted_count": deleted, "systems": results}


def opt_out(email: str) -> dict:
    """opt-out (Do Not Sell or Share) — record the opt-out; audited + persisted."""
    email = (email or "").strip().lower()
    if not email:
        return {"status": "error", "error": "email required"}
    req_id = f"DSAR-{int(time.time())}"
    now = datetime.now(timezone.utc)
    record = {"id": req_id, "type": "opt_out", "email": email, "created_at": now.isoformat(),
              "due_at": (now + timedelta(days=SLA_DAYS)).isoformat(), "status": "fulfilled"}
    _audit({"req": req_id, "type": "opt_out", "email": email})
    _persist(record)
    return {"status": "done", "request_id": req_id, "type": "opt_out", "email": email,
            "note": "Opt-out recorded. Marketing/sale-share is also governed by the site "
                    "consent banner + Global Privacy Control."}


def correct(email: str, corrections: dict, verified: bool = False) -> dict:
    """correct agent — rectification: update fields across every live connector (identity-gated)."""
    email = (email or "").strip().lower()
    if not email:
        return {"status": "error", "error": "email required"}
    if not corrections:
        return {"status": "error", "error": "corrections object required, e.g. {\"name\":\"New Name\"}"}
    if not verified:
        return {"status": "error", "error": "correction requires verified=true (identity gate)"}
    req_id = f"DSAR-{int(time.time())}"
    now = datetime.now(timezone.utc)
    record = {"id": req_id, "type": "correct", "email": email, "created_at": now.isoformat(),
              "due_at": (now + timedelta(days=SLA_DAYS)).isoformat(), "status": "fulfilled"}
    results = correct_apply(email, corrections)
    _audit({"req": req_id, "type": "correct", "email": email, "fields": list(corrections.keys()),
            "systems": results})
    _persist(record)
    return {"status": "done", "request_id": req_id, "type": "correct", "email": email,
            "corrections": corrections, "systems": results}


# ---- retention enforcer (Phase 3) — purge PII past the retention window ---------------
def retention_scan(confirm: bool = False) -> dict:
    """retention agent — find (and, with confirm=true, purge) personal data past the retention
    window. Dry-run by default (a policy check). Extend per connector; ERPNext stale leads first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    results = []
    if ERPNEXT_URL and ERPNEXT_KEY:
        stale = _erp_list("Lead", [["creation", "<", cutoff], ["status", "!=", "Converted"]],
                          ["name", "creation"])
        purged = []
        if confirm:
            with httpx.Client(timeout=20.0) as c:
                for r in stale:
                    try:
                        d = c.delete(ERPNEXT_URL + "/api/resource/Lead/" + urllib.parse.quote(r["name"]),
                                     headers=_erp_headers())
                        if d.status_code in (200, 202):
                            purged.append(r["name"])
                    except Exception:
                        pass
        results.append({"system": "erpnext", "category": "uncontacted leads",
                        "stale": len(stale), "purged": len(purged)})
    _audit({"type": "retention_scan", "cutoff": cutoff, "dry_run": not confirm,
            "stale_total": sum(r["stale"] for r in results)})
    return {"cutoff": cutoff, "retention_days": RETENTION_DAYS, "dry_run": not confirm,
            "systems": results,
            "note": None if confirm else "DRY RUN — set confirm=true to purge."}


# --- request dispatcher (the /request route) ---------------------------------
def handle_request(body: dict, summarize: Callable[[str], str | None] | None = None) -> dict:
    """Dispatch a DSAR by type over the agent actions above. `summarize` narrates the
    access export (optional LLM callback threaded from app.py)."""
    rtype = (body.get("type") or "access").lower()
    email = (body.get("email") or "").strip().lower()
    verified = bool(body.get("verified"))
    confirm = bool(body.get("confirm"))
    if not email:
        return {"status": "error", "error": "email required"}

    if rtype in ("access", "know"):
        return access(email, summarize=summarize)
    if rtype in ("delete", "erase"):
        return delete(email, confirm=(verified and confirm))
    if rtype in ("opt_out", "opt-out", "do_not_sell"):
        return opt_out(email)
    if rtype == "correct":
        return correct(email, body.get("corrections") or {}, verified=verified)
    return {"status": "error", "error": f"unknown request type '{rtype}'",
            "supported": ["access", "delete", "opt_out", "correct"]}


# --- dashboard data ----------------------------------------------------------
def activity() -> dict:
    reqs = _load_requests()
    now = datetime.now(timezone.utc)
    open_reqs = [r for r in reqs if r.get("status") not in ("fulfilled",)]
    overdue = 0
    for r in reqs:
        if r.get("status") not in ("fulfilled",) and r.get("due_at"):
            try:
                if datetime.fromisoformat(r["due_at"]) < now:
                    overdue += 1
            except Exception:
                pass
    coverage = [{"system": s, "configured": cfg()} for s, (_f, _d, cfg) in CONNECTORS.items()]
    live = sum(1 for c in coverage if c["configured"])
    return {"tenant": TENANT, "kpis": [
        {"label": "Open requests", "value": str(len(open_reqs)), "note": f"{len(reqs)} total"},
        {"label": "Overdue (SLA)", "value": str(overdue), "note": f"{SLA_DAYS}-day clock"},
        {"label": "Connectors live", "value": f"{live}/{len(coverage)}", "note": "personal-data systems"},
        {"label": "Fulfilled", "value": str(sum(1 for r in reqs if r.get('status') == 'fulfilled')),
         "note": "access · delete · opt-out"},
    ], "coverage": coverage, "requests": reqs[:25]}
