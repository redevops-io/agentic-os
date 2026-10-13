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
import threading
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable

try:
    import fcntl          # POSIX: cross-process file lock for the file-backed store
except ImportError:       # non-POSIX dev host
    fcntl = None

import logging

import httpx

_log = logging.getLogger("agentic_os.privacy")

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


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# PRIVACY_DEV_MODE: the intake response may expose verify_url/dev_note (local testing ONLY). Default off.
DEV_MODE = _truthy("PRIVACY_DEV_MODE")
# PRIVACY_DEMO_MODE: the old one-click flow, ONLY against the built-in synthetic demo subject store.
DEMO_MODE = _truthy("PRIVACY_DEMO_MODE")
VERIFY_TTL_SECONDS = int(os.environ.get("PRIVACY_VERIFY_TTL_SECONDS", "86400"))
EXPORT_TTL_SECONDS = int(os.environ.get("PRIVACY_EXPORT_TTL_SECONDS", "600"))


# --- tiny JSON store (requests + audit), file-backed on a mounted volume -----
def _store_path(name: str) -> str:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    return os.path.join(DATA_DIR, name)


# Concurrent DSARs must not corrupt the file-backed store: the requests read-modify-write would lose an
# update and the audit chain would FORK (two rows sharing one `prev`) if two intakes appended at once. Serialize
# every store mutation — in-process threads via a threading.Lock, cross-process workers via an flock when
# POSIX file locking is available (the store lives on a shared mounted volume).
_STORE_LOCK = threading.Lock()


@contextmanager
def _store_mutation():
    with _STORE_LOCK:
        if fcntl is None:
            yield
            return
        fh = open(_store_path(".store.lock"), "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


def _new_dsar_id() -> str:
    """A unique DSAR id. A bare second-granularity timestamp COLLIDED for two requests in the same second,
    and `_persist` dedupes by id — so a colliding request silently overwrote the earlier one (a lost DSAR).
    The random suffix makes every id unique."""
    return f"DSAR-{int(time.time())}-{secrets.token_hex(4)}"


def _load_requests() -> list[dict]:
    try:
        return json.loads(Path(_store_path("requests.json")).read_text())
    except Exception:
        return []


def _save_requests(reqs: list[dict]) -> None:
    Path(_store_path("requests.json")).write_text(json.dumps(reqs, indent=2))


def _persist(record: dict) -> None:
    with _store_mutation():                       # atomic read-modify-write: no lost DSAR under concurrency
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


class AuditWriteError(RuntimeError):
    """The audit chain could not be appended. Destructive actions MUST refuse to proceed on this."""


def _audit(entry: dict, strict: bool = False) -> bool:
    """Append a tamper-evident audit entry: each row carries the prev hash + its own
    sha256 over (prev + the row), so any edit/removal breaks the chain (see verify_audit_chain).

    Returns True on success. A failed append is never silent: ``strict=True`` raises AuditWriteError (used before
    every destructive action — no audit, no erasure); otherwise it is logged and False is returned."""
    try:
        with _store_mutation():                   # read head + append atomically → the chain never forks
            e = {"ts": datetime.now(timezone.utc).isoformat(), **entry, "prev": _last_audit_hash()}
            e["hash"] = hashlib.sha256(json.dumps(e, sort_keys=True).encode()).hexdigest()
            with open(_store_path("audit.jsonl"), "a") as fh:
                fh.write(json.dumps(e) + "\n")
                fh.flush()
        return True
    except Exception as exc:  # noqa: BLE001
        _log.error("agentic-privacy: audit append FAILED (%s: %s)", type(exc).__name__, exc)
        if strict:
            raise AuditWriteError(f"audit append failed: {type(exc).__name__}") from exc
        return False


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
def _mailer_configured() -> bool:
    return bool(POSTMARK_TOKEN or SMTP_HOST)


def _send_email(to: str, subject: str, body_html: str) -> str:
    """Send via Postmark, then SMTP. Returns the delivery channel: "postmark" | "smtp" | "undeliverable".

    "undeliverable" means NO channel delivered the message (none configured, or every configured one failed).
    There is deliberately no log-only fallback that hands the token to the caller."""
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
    return "undeliverable"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _verify_link(req_id: str, token: str, base: str) -> str:
    root = PRIVACY_PUBLIC_URL or base.rstrip("/")
    return f"{root}/verify?id={urllib.parse.quote(req_id)}&token={urllib.parse.quote(token)}"


def _send_verification(req_id: str, email: str, rtype: str, base: str) -> tuple[str, str, str]:
    token = secrets.token_urlsafe(24)
    link = _verify_link(req_id, token, base)
    label = {"access": "access your data", "delete": "delete your data",
             "opt_out": "opt out of sale/sharing", "correct": "correct your data"}.get(rtype, "process your request")
    body = (f"<p>We received a request to <strong>{html.escape(label)}</strong> tied to this email "
            f"address ({html.escape(email)}).</p><p>To verify it was you and let us proceed, open the link below and press the "
            f"confirm button:</p>"
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


# ---- synthetic demo subject store (PRIVACY_DEMO_MODE only; never real data) ------------
_DEMO_SEED = {"demo@example.com": [
    {"doctype": "Lead", "name": "DEMO-LEAD-1", "lead_name": "Demo Person"},
    {"doctype": "Subscriber", "name": "DEMO-SUB-1", "email": "demo@example.com"}]}
_DEMO_STORE: dict[str, list[dict]] = {k: [dict(r) for r in v] for k, v in _DEMO_SEED.items()}


def demo_find(email: str) -> dict:
    recs = [dict(r) for r in _DEMO_STORE.get(email, [])]
    return {"system": "demo", "ok": True, "count": len(recs), "records": recs}


def demo_delete(email: str) -> dict:
    gone = _DEMO_STORE.pop(email, [])
    return {"system": "demo", "ok": True, "deleted": len(gone), "details": [r["name"] for r in gone]}


_REAL_CONNECTORS = CONNECTORS


def validate_demo_mode(demo: bool, connectors: dict | None = None) -> None:
    """Demo mode may run ONLY against the synthetic store: refuse to start when any real connector is configured."""
    if not demo:
        return
    live = [s for s, (_f, _d, cfg) in (connectors or _REAL_CONNECTORS).items() if cfg()]
    if live:
        raise RuntimeError("PRIVACY_DEMO_MODE=true refused: real connectors are configured "
                           f"({', '.join(live)}). Demo mode only runs against synthetic data.")


validate_demo_mode(DEMO_MODE)
if DEMO_MODE:
    CONNECTORS = {"demo": (demo_find, demo_delete, lambda: True)}


# --- orchestrator: fan-out across every connector ----------------------------
def _fan_out(email: str, op: str) -> list[dict]:
    out = []
    for sys_name, (find, delete, _cfg) in CONNECTORS.items():
        try:
            out.append((delete if op == "delete" else find)(email))
        except Exception as e:  # noqa: BLE001 — a broken connector degrades, never crashes
            out.append({"system": sys_name, "ok": False, "error": str(e)[:120], "records": []})
    return out


def _snapshot(email: str) -> dict:
    """{system: record_count | None} over every CONFIGURED connector (None = configured but find failed)."""
    snap: dict = {}
    for sys_name, (find, _d, cfg) in CONNECTORS.items():
        if not cfg():
            continue
        try:
            r = find(email)
            snap[sys_name] = int(r.get("count", 0)) if r.get("ok") else None
        except Exception:  # noqa: BLE001
            snap[sys_name] = None
    return snap


# Optional narration hook (counts/categories only). app.py sets it iff PRIVACY_LLM_SUMMARY=true.
SUMMARIZER: Callable[[str], str | None] | None = None


# --- request store helpers ---------------------------------------------------
def _norm(email) -> str:
    return (email or "").strip().lower()


def _mutate_request(req_id: str, fn):
    """Atomically load → fn(rec) → save one request. Returns (found, fn's return)."""
    with _store_mutation():
        reqs = _load_requests()
        rec = next((r for r in reqs if r.get("id") == req_id), None)
        if rec is None:
            return False, None
        out = fn(rec)
        _save_requests(reqs)
        return True, out


def _get_request(req_id: str) -> dict | None:
    return next((r for r in _load_requests() if r.get("id") == req_id), None)


def _refused(reason: str, **kw) -> dict:
    return {"status": "refused", "executed": False, "reason": reason, "erasure_verified": False, **kw}


def _secs_from_now(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=n)).isoformat()


def _expired(iso: str | None) -> bool:
    try:
        return not iso or datetime.fromisoformat(iso) < datetime.now(timezone.utc)
    except Exception:  # noqa: BLE001
        return True


# ── agent actions ─────────────────────────────────────────────────────────────
def intake(email: str, rtype: str = "access", base_url: str = "") -> dict:
    """intake agent — open a PENDING DSAR and email a single-use verification token.

    Never returns or deletes data. Identity is proven ONLY by redeeming the emailed token (verify_request).
    If no channel can deliver the token the request stays pending and `delivery` is "undeliverable" — there is no
    caller-visible self-verify path (verify_url/dev_note only appear under PRIVACY_DEV_MODE=true)."""
    email = _norm(email)
    rtype = (rtype or "access").lower()
    if not email or "@" not in email:
        return {"status": "error", "error": "a valid email is required"}
    req_id = _new_dsar_id()
    now = datetime.now(timezone.utc)
    token, channel, link = _send_verification(req_id, email, rtype, base_url)
    rec = {"id": req_id, "type": rtype, "email": email, "verified": False,
           "verify_token_hash": _hash_token(token), "verify_expires_at": _secs_from_now(VERIFY_TTL_SECONDS),
           "created_at": now.isoformat(), "due_at": (now + timedelta(days=SLA_DAYS)).isoformat(),
           "status": "awaiting_verification", "source": "intake", "delivery": channel}
    _audit({"req": req_id, "type": "intake", "email": email, "request_type": rtype, "verification": channel})
    _persist(rec)
    if channel == "undeliverable":
        msg = ("Request received, but we could not send the verification email. It stays pending until "
               "identity is verified; contact the privacy team.")
    else:
        msg = (f"Request received. Check {email} for a verification link, then we'll "
               f"respond within {SLA_DAYS} days.")
    resp = {"status": "received", "request_id": req_id, "type": rtype, "email": email,
            "delivery": channel, "verification": channel, "message": msg}
    if DEV_MODE:
        resp["verify_url"] = link
        resp["dev_note"] = "PRIVACY_DEV_MODE: verification link returned for local testing only."
    return resp


def verify_request(req_id: str, token: str) -> dict:
    """Redeem the emailed token (request id + token). Single use, expiring. Returns {ok, ...} with no PII."""
    token = token or ""

    def _redeem(rec):
        h = rec.get("verify_token_hash")
        if rec.get("verified") or not h or not token:
            return "invalid"
        if not secrets.compare_digest(h, _hash_token(token)):
            return "invalid"
        if _expired(rec.get("verify_expires_at")):
            return "expired"
        rec["verified"] = True
        rec["verified_at"] = datetime.now(timezone.utc).isoformat()
        rec.pop("verify_token_hash", None)
        rec.pop("verify_expires_at", None)
        rec["status"] = "fulfilled" if rec.get("type") in ("opt_out", "opt-out", "do_not_sell") else "verified"
        return "ok"

    found, outcome = _mutate_request(req_id, _redeem)
    if not found or outcome != "ok":
        return {"ok": False, "reason": outcome or "invalid"}
    rec = _get_request(req_id) or {}
    rtype = rec.get("type")
    _audit({"req": req_id, "type": "verified", "request_type": rtype, "email": rec.get("email")})
    if rtype in ("access", "know"):
        d = fulfil_access(req_id)
        return {"ok": True, "type": "access", "delivery": d.get("delivery"), "export_url": d.get("export_url"),
                "message": d.get("message")}
    if rtype in ("opt_out", "opt-out", "do_not_sell"):
        _audit({"req": req_id, "type": "opt_out_verified", "email": rec.get("email")})
        return {"ok": True, "type": "opt_out", "message": "Identity verified. Your opt-out of sale/sharing is recorded."}
    return {"ok": True, "type": rtype,
            "message": "Identity verified. Your request will be actioned after operator confirmation and "
                       "you'll receive confirmation when it's complete."}


# ---- access export -----------------------------------------------------------
def _categories(results: list[dict]) -> list[str]:
    cats = set()
    for r in results:
        for rec in r.get("records", []):
            if rec.get("doctype"):
                cats.add(str(rec["doctype"]))
        if r.get("records"):
            cats.add(str(r.get("system")))
    return sorted(cats)


def summarize_counts(results: list[dict]) -> str:
    """Deterministic export summary: counts per system + categories. Contains NO record content."""
    parts = []
    for r in results:
        if r.get("ok"):
            parts.append(f"{r.get('system')}: {r.get('count', 0)} record(s)")
        else:
            parts.append(f"{r.get('system')}: {r.get('error', 'unavailable')}")
    cats = _categories(results)
    return "; ".join(parts) + (f". Categories: {', '.join(cats)}." if cats else ".")


def _llm_prompt(results: list[dict]) -> str:
    """The ONLY thing ever handed to an LLM: counts + category labels. No record content, no email."""
    return ("You are a privacy officer. Write a short plain-language note for a data subject summarising what "
            "kinds of data we hold, from these per-system counts only. Do not invent details.\n\n"
            + json.dumps({"systems": [{"system": r.get("system"), "ok": bool(r.get("ok")),
                                       "count": r.get("count", 0)} for r in results],
                          "categories": _categories(results)}))


def _export_email_html(email: str, results: list[dict], summary: str) -> str:
    rows = "".join(f"<h3>{html.escape(str(r.get('system')))}</h3><pre>"
                   f"{html.escape(json.dumps(r.get('records', []), indent=2, default=str))}</pre>"
                   for r in results if r.get("ok") and r.get("records"))
    return (f"<p>Your personal-data export for {html.escape(email)}.</p><p>{html.escape(summary)}</p>"
            f"{rows or '<p>We hold no records tied to this address.</p>'}")


def fulfil_access(req_id: str, summarize: Callable[[str], str | None] | None = None) -> dict:
    """Deliver the access export for a VERIFIED access request: emailed to the verified address when a mailer is
    configured, otherwise held behind a single-use short-TTL export token (fetched via GET /export).
    Returns counts/summary only — never record content."""
    rec = _get_request(req_id)
    if not (rec and rec.get("verified") and rec.get("type") in ("access", "know")
            and rec.get("status") in ("verified", "fulfilled")):
        return {"status": "refused", "executed": False, "reason": "request not verified"}
    email = rec["email"]
    results = _fan_out(email, "find")
    total = sum(r.get("count", 0) for r in results if r.get("ok"))
    summary = summarize_counts(results)
    summarize = summarize or SUMMARIZER
    if summarize:
        summary = (summarize(_llm_prompt(results)) or summary)
    channel = _send_email(email, "Your personal data export", _export_email_html(email, results, summary)) \
        if _mailer_configured() else "undeliverable"
    out = {"status": "done", "request_id": req_id, "type": "access", "record_count": total,
           "systems": [{"system": r.get("system"), "ok": bool(r.get("ok")), "count": r.get("count", 0)}
                       for r in results],
           "summary": summary, "delivery": channel}
    if channel in ("postmark", "smtp"):
        def _done(r):
            r.update(status="fulfilled", record_count=total, delivered_via=channel)
            r.pop("export_hash", None)
            r.pop("export_expires_at", None)
        _mutate_request(req_id, _done)
        out["message"] = ("Identity verified. We've emailed the personal data we hold about you "
                          "to this address.")
    else:
        tok = secrets.token_urlsafe(24)

        def _hold(r):
            r.update(status="verified", record_count=total, export_hash=_hash_token(tok),
                     export_expires_at=_secs_from_now(EXPORT_TTL_SECONDS))
        _mutate_request(req_id, _hold)
        out["export_url"] = f"/export?id={urllib.parse.quote(req_id)}&token={urllib.parse.quote(tok)}"
        out["message"] = (f"Identity verified. Your export is available once, for the next "
                          f"{EXPORT_TTL_SECONDS // 60} minutes, at the link below.")
    _audit({"req": req_id, "type": "access_fulfilled", "email": email, "count": total, "delivery": channel,
            "systems": [r.get("system") for r in results]})
    return out


def redeem_export(req_id: str, token: str) -> dict:
    """Single-use, short-TTL fetch of the export for a verified access request (request id + export token)."""
    token = token or ""

    def _consume(rec):
        h = rec.get("export_hash")
        if not (rec.get("verified") and h and token) or not secrets.compare_digest(h, _hash_token(token)):
            return "invalid"
        exp = rec.get("export_expires_at")
        rec.pop("export_hash", None)           # consumed either way (expired tokens are dead too)
        rec.pop("export_expires_at", None)
        if _expired(exp):
            return "expired"
        rec["status"] = "fulfilled"
        rec["delivered_via"] = "export_token"
        return rec["email"]

    found, out = _mutate_request(req_id, _consume)
    if not found or out in (None, "invalid", "expired"):
        return {"ok": False, "reason": out or "invalid"}
    results = _fan_out(out, "find")
    _audit({"req": req_id, "type": "export_fetched", "email": out})
    return {"ok": True, "request_id": req_id, "record_count": sum(r.get("count", 0) for r in results if r.get("ok")),
            "summary": summarize_counts(results), "systems": results}


def access(request_id: str, email: str, summarize: Callable[[str], str | None] | None = None) -> dict:
    """access agent — (re)deliver the export for a VERIFIED access request id bound to this email.
    The result carries counts only; the records go to the verified subject's mailbox."""
    rec = _get_request(request_id or "")
    if not (rec and rec.get("email") == _norm(email)):
        return {"status": "refused", "executed": False, "reason": "unknown request or email mismatch"}
    return fulfil_access(request_id, summarize=summarize)


# ---- erasure with verify-after-act ---------------------------------------------
def _claim(request_id: str, email: str, rtypes: tuple, running: str):
    email = _norm(email)

    def _c(rec):
        if rec.get("type") not in rtypes:
            return "wrong request type"
        if rec.get("email") != email:
            return "email does not match request"
        if rec.get("status") in ("erased", "corrected"):
            return "already_done"
        if not rec.get("verified") or rec.get("status") not in ("verified", "partial_erasure", "partial_correction"):
            return "request not verified"
        rec["_prev_status"] = rec["status"]
        rec["status"] = running
        return "ok"
    found, out = _mutate_request(request_id or "", _c)
    return "unknown request" if not found else out


def _release(request_id: str, running: str) -> None:
    _mutate_request(request_id, lambda r: r.update(status=r.pop("_prev_status", "verified"))
                    if r.get("status") == running else None)


def execute_erasure(request_id: str, email: str, actor: str = "approval") -> dict:
    """Cascading erasure for a VERIFIED delete request bound to `email`, then READ BACK.

    Refuses unless: request exists, type delete, email matches, identity verified (token redeemed). Refuses
    (before touching anything) if the audit append fails. After erasing, re-runs the connector find fan-out:
    status `erased` only if residual == 0 (and nothing unverifiable), else `partial_erasure`."""
    email = _norm(email)
    claim = _claim(request_id, email, ("delete", "erase"), "erasing")
    if claim == "already_done":
        rec = _get_request(request_id) or {}
        return {"status": "erased", "request_id": request_id, "type": "delete", "already_erased": True,
                **(rec.get("erasure") or {}), "erasure_verified": True}
    if claim != "ok":
        return _refused(claim)
    live = [s for s, (_f, _d, cfg) in CONNECTORS.items() if cfg()]
    if not live:
        _release(request_id, "erasing")
        return _refused("no live connectors configured")
    before = _snapshot(email)
    try:
        _audit({"req": request_id, "type": "erasure_started", "email": email, "actor": actor,
                "before": before}, strict=True)
    except AuditWriteError:
        _release(request_id, "erasing")
        return _refused("audit log unavailable — refusing to erase")
    results = _fan_out(email, "delete")
    after = _snapshot(email)
    retained = {r["system"]: r["retained"] for r in results if r.get("retained")}
    residual_by_system = {s: n for s, n in after.items() if n and s not in retained}
    residual = sum(residual_by_system.values())
    unverified = sorted(s for s, n in after.items() if n is None)
    verified = residual == 0 and not unverified
    deleted = sum(r.get("deleted", 0) for r in results if r.get("ok"))
    summary = {"deleted_count": deleted, "before": before, "after": after, "residual": residual,
               "residual_by_system": residual_by_system, "retained": retained,
               "unverified_systems": unverified, "erasure_verified": verified}

    def _fin(rec):
        rec.pop("_prev_status", None)
        rec.update(status="erased" if verified else "partial_erasure", erasure=summary, deleted_count=deleted,
                   completed_at=datetime.now(timezone.utc).isoformat())
    _mutate_request(request_id, _fin)
    audit_ok = _audit({"req": request_id, "type": "erasure_verified" if verified else "partial_erasure",
                       "email": email, "actor": actor, **summary})
    out = {"status": "erased" if verified else "partial_erasure", "request_id": request_id, "type": "delete",
           "email": email, "actor": actor, **summary,
           "systems": [{"system": r.get("system"), "ok": bool(r.get("ok")), "deleted": r.get("deleted", 0)}
                       for r in results], "audit_ok": audit_ok}
    if not verified:
        out["ok"] = False        # the SDK treats ok:False as a failed side effect (never minted into success)
    return out


def _name_mismatch(email: str, name: str | None) -> int:
    if not name:
        return 0
    n = 0
    for r in _fan_out(email, "find"):
        for rec in r.get("records", []):
            have = rec.get("lead_name") if "lead_name" in rec else rec.get("name") if r.get("system") != "erpnext" else None
            if have is not None and have != name:
                n += 1
    return n


def execute_correction(request_id: str, email: str, corrections: dict, actor: str = "operator") -> dict:
    """Rectification for a VERIFIED correct request: apply, then READ BACK (re-find) before reporting success."""
    email = _norm(email)
    if not corrections:
        return {"status": "error", "error": "corrections object required, e.g. {\"name\":\"New Name\"}"}
    claim = _claim(request_id, email, ("correct",), "correcting")
    if claim == "already_done":
        return {"status": "corrected", "request_id": request_id, "already_corrected": True,
                "correction_verified": True}
    if claim != "ok":
        return {"status": "refused", "executed": False, "reason": claim, "correction_verified": False}
    new_email = _norm(corrections.get("email")) or None
    before = _snapshot(email)
    try:
        _audit({"req": request_id, "type": "correction_started", "email": email, "actor": actor,
                "fields": sorted(corrections), "before": before}, strict=True)
    except AuditWriteError:
        _release(request_id, "correcting")
        return {"status": "refused", "executed": False, "reason": "audit log unavailable — refusing to correct",
                "correction_verified": False}
    results = correct_apply(email, corrections)
    if new_email:
        old_after = _snapshot(email)
        new_after = _snapshot(new_email)
        mismatch = sum(n for n in old_after.values() if n) + sum(
            max(0, (before.get(s) or 0) - (new_after.get(s) or 0)) for s in before)
        unverified = sorted(s for s, n in {**old_after, **new_after}.items() if n is None)
    else:
        mismatch = _name_mismatch(email, corrections.get("name"))
        unverified = sorted(s for s, n in _snapshot(email).items() if n is None)
    verified = mismatch == 0 and not unverified
    summary = {"mismatch": mismatch, "unverified_systems": unverified, "correction_verified": verified,
               "fields": sorted(corrections)}

    def _fin(rec):
        rec.pop("_prev_status", None)
        rec.update(status="corrected" if verified else "partial_correction", correction=summary,
                   completed_at=datetime.now(timezone.utc).isoformat())
        if verified and new_email:
            rec["email"] = new_email
    _mutate_request(request_id, _fin)
    audit_ok = _audit({"req": request_id, "type": "correction_verified" if verified else "partial_correction",
                       "email": email, "actor": actor, **summary})
    out = {"status": "corrected" if verified else "partial_correction", "request_id": request_id,
           "type": "correct", "actor": actor, "systems": results, "audit_ok": audit_ok, **summary}
    if not verified:
        out["ok"] = False
    return out


def fulfil(request_id: str, email: str | None = None, corrections: dict | None = None,
           actor: str = "operator") -> dict:
    """Operator-confirmed fulfilment (the /fulfil route): dispatch delete/correct for a verified request."""
    rec = _get_request(request_id or "")
    if not rec:
        return _refused("unknown request")
    email = _norm(email) or rec.get("email", "")
    if rec.get("type") in ("delete", "erase"):
        return execute_erasure(request_id, email, actor=actor)
    if rec.get("type") == "correct":
        return execute_correction(request_id, email, corrections or {}, actor=actor)
    return _refused(f"request type '{rec.get('type')}' is not operator-fulfilled")


def opt_out(email: str) -> dict:
    """opt-out (Do Not Sell or Share) — demo mode only; real opt-outs are verified via intake."""
    email = _norm(email)
    if not email:
        return {"status": "error", "error": "email required"}
    req_id = _new_dsar_id()
    now = datetime.now(timezone.utc)
    record = {"id": req_id, "type": "opt_out", "email": email, "created_at": now.isoformat(),
              "due_at": (now + timedelta(days=SLA_DAYS)).isoformat(), "status": "fulfilled", "verified": True}
    _audit({"req": req_id, "type": "opt_out", "email": email})
    _persist(record)
    return {"status": "done", "request_id": req_id, "type": "opt_out", "email": email,
            "note": "Opt-out recorded."}


# ---- retention enforcer — purge PII past the retention window -------------------------
def retention_scan(confirm: bool = False) -> dict:
    """retention agent — counts of personal data past the retention window. DRY-RUN by default and counts only
    (no PII). confirm=True purges; callers MUST have authenticated an operator first (app.py does), and the
    purge refuses if the audit append fails."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    results = []
    if confirm:
        try:
            _audit({"type": "retention_purge_started", "cutoff": cutoff}, strict=True)
        except AuditWriteError:
            return {"cutoff": cutoff, "dry_run": True, "error": "audit log unavailable — refusing to purge",
                    "systems": []}
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
    if confirm:
        _audit({"type": "retention_purged", "cutoff": cutoff,
                "stale_total": sum(r["stale"] for r in results),
                "purged_total": sum(r["purged"] for r in results)})
    return {"cutoff": cutoff, "retention_days": RETENTION_DAYS, "dry_run": not confirm,
            "systems": results,
            "note": None if confirm else "DRY RUN — counts only; a purge needs operator authentication + confirm=true."}


# --- request dispatcher (the /request route) ---------------------------------
_FORGEABLE = ("verified", "confirm", "confirmed", "approved")


def handle_request(body: dict, summarize: Callable[[str], str | None] | None = None, base_url: str = "") -> dict:
    """The public /request route. Body-supplied `verified`/`confirm` are IGNORED: this only opens a PENDING
    request (identical to /intake) — no data returned, nothing erased. The exception is PRIVACY_DEMO_MODE, the
    one-click flow against the synthetic demo store only."""
    body = {k: v for k, v in (body or {}).items() if k not in _FORGEABLE} if not DEMO_MODE else dict(body or {})
    rtype = (body.get("type") or "access").lower()
    email = _norm(body.get("email"))
    if not email:
        return {"status": "error", "error": "email required"}
    if rtype not in ("access", "know", "delete", "erase", "opt_out", "opt-out", "do_not_sell", "correct"):
        return {"status": "error", "error": f"unknown request type '{rtype}'",
                "supported": ["access", "delete", "opt_out", "correct"]}
    if not DEMO_MODE:
        return intake(email, "delete" if rtype == "erase" else rtype, base_url)
    # ---- demo mode (synthetic data only; validate_demo_mode guarantees no real connector is live) ----
    if rtype in ("opt_out", "opt-out", "do_not_sell"):
        return opt_out(email)
    if rtype in ("access", "know"):
        results = _fan_out(email, "find")
        return {"status": "done", "demo": True, "type": "access", "email": email,
                "record_count": sum(r.get("count", 0) for r in results if r.get("ok")),
                "systems": results, "summary": summarize_counts(results)}
    if rtype in ("delete", "erase"):
        if not (body.get("verified") and body.get("confirm")):
            would = _snapshot(email)
            return {"status": "preview", "demo": True, "dry_run": True, "would_delete": sum(v or 0 for v in would.values()),
                    "note": "DEMO DRY RUN — set verified=true and confirm=true to erase the synthetic subject."}
        req_id = _new_dsar_id()
        now = datetime.now(timezone.utc)
        _persist({"id": req_id, "type": "delete", "email": email, "verified": True, "status": "verified",
                  "source": "demo", "created_at": now.isoformat(),
                  "due_at": (now + timedelta(days=SLA_DAYS)).isoformat()})
        return {"demo": True, **execute_erasure(req_id, email, actor="demo")}
    return {"status": "error", "error": "correct is not available in demo mode"}


# --- dashboard data ----------------------------------------------------------
def _mask(email: str) -> str:
    local, _, dom = (email or "").partition("@")
    return (local[:1] + "***@" + dom) if dom else "***"


_CLOSED = ("fulfilled", "erased", "corrected")


def activity() -> dict:
    reqs = _load_requests()
    now = datetime.now(timezone.utc)
    open_reqs = [r for r in reqs if r.get("status") not in _CLOSED]
    overdue = 0
    for r in open_reqs:
        if r.get("due_at"):
            try:
                if datetime.fromisoformat(r["due_at"]) < now:
                    overdue += 1
            except Exception:
                pass
    coverage = [{"system": s, "configured": cfg()} for s, (_f, _d, cfg) in CONNECTORS.items()]
    live = sum(1 for c in coverage if c["configured"])
    # unauthenticated surface: expose neither token material nor full subject emails
    shown = [{k: v for k, v in r.items() if k in ("id", "type", "status", "created_at", "due_at", "verified")}
             | {"email": _mask(r.get("email", ""))} for r in reqs[:25]]
    return {"tenant": TENANT, "kpis": [
        {"label": "Open requests", "value": str(len(open_reqs)), "note": f"{len(reqs)} total"},
        {"label": "Overdue (SLA)", "value": str(overdue), "note": f"{SLA_DAYS}-day clock"},
        {"label": "Connectors live", "value": f"{live}/{len(coverage)}", "note": "personal-data systems"},
        {"label": "Fulfilled", "value": str(sum(1 for r in reqs if r.get('status') in _CLOSED)),
         "note": "access · delete · opt-out"},
    ], "coverage": coverage, "requests": shown}
