"""DR-01: the privacy portal's public submission (``POST /intake``, ``POST /request``), pinned as the constrained exception it is.

It needs no grant (data subjects must be able to reach it) and does exactly one thing: open a PENDING intake request and send ONE verification e-mail to the address the subject typed. Strict request schema, a body
size limit, per-client / per-address / global caps with bounded memory, a honeypot, a fixed Postmark destination and operation (no recipient list, no subject or header injection, no Host-derived link),
identical responses whoever the address belongs to (no customer-record lookup at all), minimal retention, a redacted audit event. It never discloses, changes or deletes a customer record.
"""
from __future__ import annotations

import importlib
import json
import time

import pytest
from fastapi.testclient import TestClient

core = importlib.import_module("agentic-privacy.core")
appmod = importlib.import_module("agentic-privacy.app")

SUBJECT = "jane@example.com"


class Spy:
    def __init__(self):
        self.finds, self.deletes, self.posts = [], [], []


@pytest.fixture()
def env(monkeypatch, tmp_path):
    spy = Spy()

    def conn(system):
        return (lambda email: spy.finds.append((system, email)) or {"system": system, "ok": True, "count": 1, "records": [{"email": email}]},
                lambda email: spy.deletes.append((system, email)) or {"system": system, "ok": True, "deleted": 1},
                lambda: True)
    monkeypatch.setattr(core, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(core, "CONNECTORS", {"crm": conn("crm")})
    monkeypatch.setattr(core, "DEV_MODE", False)
    monkeypatch.setattr(core, "DEMO_MODE", False)
    monkeypatch.setattr(core, "SMTP_HOST", "")
    monkeypatch.setattr(core, "POSTMARK_TOKEN", "pm-token")
    monkeypatch.setattr(core, "PRIVACY_PUBLIC_URL", "https://privacy.example.org")
    monkeypatch.setattr(core, "PRIVACY_FROM_EMAIL", "privacy@example.org")

    class Resp:
        status_code = 200
    monkeypatch.setattr(core.httpx, "post", lambda url, **kw: spy.posts.append((url, kw)) or Resp())
    for k in ("PRIVACY_INTAKE_GLOBAL_PER_HOUR", "PRIVACY_PENDING_MAX", "PRIVACY_INTAKE_MAX_BODY", "PRIVACY_TRUST_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PRIVACY_INTAKE_PER_EMAIL_HOUR", "10000")
    monkeypatch.setenv("PRIVACY_RATE_PER_MIN", "100000")
    monkeypatch.setattr(appmod, "RATE_BURST", 100000)
    appmod._reset_rate_limits()
    spy.client = TestClient(appmod.app, raise_server_exceptions=False)
    spy.tmp = tmp_path
    return spy


def post(env, path="/intake", body=None, **kw):
    return env.client.post(path, json=body if body is not None else {"email": SUBJECT, "type": "access"}, **kw)


# ── what it does ──

def test_it_opens_one_pending_request_and_sends_one_fixed_postmark_message(env):
    r = post(env)
    assert r.status_code == 200 and r.json()["status"] == "received" and set(r.json()) <= {"status", "request_id", "type", "delivery", "message"}
    assert len(env.posts) == 1
    url, kw = env.posts[0]
    assert url == "https://api.postmarkapp.com/email"                                          # a fixed destination
    assert set(kw["json"]) == {"From", "To", "Subject", "HtmlBody", "MessageStream"} and kw["json"]["To"] == SUBJECT
    assert kw["json"]["Subject"] == "Verify your privacy request" and kw["json"]["From"] == "privacy@example.org" and kw["json"]["MessageStream"] == "outbound"
    assert "https://privacy.example.org/verify?id=" in kw["json"]["HtmlBody"]
    recs = core._load_requests()
    assert len(recs) == 1 and recs[0]["status"] == "awaiting_verification" and recs[0]["verified"] is False and recs[0]["source"] == "public_intake"
    assert env.finds == [] and env.deletes == []                                               # no customer record was looked at, changed or deleted


def test_the_response_echoes_nothing_the_caller_typed_and_carries_no_token(env):
    r = post(env, body={"email": "Jane.Doe+x@Example.com", "type": "delete"})
    text = r.text.lower()
    assert "jane" not in text and "example.com" not in text and "token" not in text and "verify" not in text.replace("verification", "")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff" and r.headers["content-type"].startswith("application/json")


def test_responses_are_identical_whether_or_not_the_address_is_a_customer(env):
    known = post(env, body={"email": SUBJECT, "type": "access"})                                # SUBJECT is in the fake CRM
    unknown = post(env, body={"email": "nobody-at-all@example.net", "type": "access"})
    a, b = known.json(), unknown.json()
    a.pop("request_id"), b.pop("request_id")
    assert known.status_code == unknown.status_code and a == b and env.finds == []             # and no lookup happened to differ on


# ── strict schema, size, content type ──

@pytest.mark.parametrize("body", [[], "x", 5, None, [{"email": SUBJECT}], {"email": SUBJECT, "type": "access", "verified": True}, {"email": SUBJECT, "confirm": True},
                                  {"email": SUBJECT, "_approval": {"approved": True}}, {"email": SUBJECT, "extra": 1}, {"type": "access"}, {"email": ""},
                                  {"email": 5}, {"email": ["a@b.co"]}, {"email": {"a": 1}}, {"email": SUBJECT, "type": 1}, {"email": SUBJECT, "type": "purge"},
                                  {"email": SUBJECT, "type": ["access"]}, {"email": SUBJECT, "website": 1}])
@pytest.mark.parametrize("path", ["/intake", "/request"])
def test_a_body_that_is_not_exactly_the_schema_is_a_400_and_does_nothing(env, path, body):
    r = env.client.post(path, content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["status"] == "error", (path, body, r.text)
    assert env.posts == [] and core._load_requests() == []


@pytest.mark.parametrize("email", ["a@b", "a@@b.co", "a b@example.com", "a@exa mple.com", "a@example.com\r\nBcc: x@y.co", "a@example.com\nBcc: x@y.co", "a@example.com,b@example.com",
                                   "a@example.com;b@example.com", "<a@example.com>", "\"a\" <a@example.com>", "a@example.com>", "a@exam\x00ple.com", "a@example.com\t", "é@example.com",
                                   "a@exämple.com", "a@example..com", ".a@example.com", "a." + "x" * 70 + "@example.com", "a@" + "x" * 250 + ".com", "a@-example.com", "a@example.com " + "x" * 300,
                                   "a@example.com ", "a@example.com​", "a@[127.0.0.1]", "a@localhost", "@example.com", "a@", "mailto:a@example.com", "a@example.com%0d%0aBcc:x@y.co"])
def test_an_address_that_is_not_one_plain_mailbox_is_refused_before_any_send(env, email):
    r = post(env, body={"email": email, "type": "access"})
    assert r.status_code == 400 and env.posts == [] and core._load_requests() == []


@pytest.mark.parametrize("rtype", ["access\r\nSubject: pwned", "access\nBcc: x@y.co", "ACCESS ", "delete\x00", "know\r\n", "../access", "access;delete"])
def test_the_request_type_is_a_closed_set_so_it_cannot_inject_into_the_message(env, rtype):
    r = post(env, body={"email": SUBJECT, "type": rtype})
    assert r.status_code == 400 and env.posts == []


@pytest.mark.parametrize("rtype,expected", [("access", "access"), ("delete", "delete"), ("opt_out", "opt_out"), ("correct", "correct")])
def test_the_four_request_types_are_accepted(env, rtype, expected):
    r = post(env, body={"email": SUBJECT, "type": rtype})
    assert r.status_code == 200 and r.json()["type"] == expected


def test_a_missing_type_defaults_to_access(env):
    assert post(env, body={"email": SUBJECT}).json()["type"] == "access"


def test_the_body_size_is_limited(env):
    big = json.dumps({"email": SUBJECT, "type": "access", "website": "x" * 5000})
    r = env.client.post("/intake", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and env.posts == []
    r = env.client.post("/request", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413

    def chunks():                                                                              # no Content-Length: the limit is enforced while reading
        for _ in range(100):
            yield b"x" * 100
    r = env.client.post("/intake", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and env.posts == []


def test_the_content_type_must_be_json(env):
    for ct in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", ""):
        r = env.client.post("/intake", content=json.dumps({"email": SUBJECT}), headers={"Content-Type": ct} if ct else {})
        assert r.status_code == 415, ct
    assert env.posts == []


# ── the message: fixed operation, no injection, no Host-derived link ──

def test_the_send_primitive_itself_refuses_a_recipient_that_is_not_one_plain_mailbox(env):
    for to in ("a@example.com\r\nBcc: x@y.co", "a@example.com,b@example.com", "<a@example.com>", "a@example.com\n", "a b@example.com", ""):
        assert core._send_email(to, "Verify your privacy request", "<p>x</p>") == "undeliverable"
    assert env.posts == []


def test_the_link_in_the_message_never_comes_from_the_host_header(env, monkeypatch):
    r = post(env, headers={"Host": "evil.example"})
    body = env.posts[0][1]["json"]["HtmlBody"]
    assert "evil.example" not in body and "https://privacy.example.org/verify" in body
    monkeypatch.setattr(core, "PRIVACY_PUBLIC_URL", "")                                        # no configured public URL: a Host header must not become the link
    env.posts.clear()
    r = post(env, headers={"Host": "evil.example"})
    assert env.posts == [] and r.status_code == 200 and r.json()["delivery"] == "undeliverable"
    env.posts.clear()
    r = env.client.post("http://localhost/intake", json={"email": SUBJECT, "type": "access"})   # a loopback host is the local development path
    assert env.posts and "http://localhost/verify" in env.posts[0][1]["json"]["HtmlBody"]


# ── abuse controls ──

def test_the_honeypot_looks_like_success_and_does_nothing(env):
    honest = post(env)
    bot = post(env, body={"email": "bot@example.com", "type": "access", "website": "http://spam.example"})
    a, b = honest.json(), bot.json()
    a.pop("request_id"), b.pop("request_id")
    assert bot.status_code == honest.status_code and a == b
    assert len(env.posts) == 1 and len(core._load_requests()) == 1


def test_a_global_cap_bounds_the_e_mails_whoever_asks(env, monkeypatch):
    monkeypatch.setenv("PRIVACY_INTAKE_GLOBAL_PER_HOUR", "3")
    monkeypatch.setenv("PRIVACY_TRUST_PROXY", "true")
    codes = [post(env, body={"email": f"u{i}@example.com", "type": "access"}, headers={"X-Forwarded-For": f"9.9.9.{i}"}).status_code for i in range(6)]
    assert codes == [200, 200, 200, 429, 429, 429] and len(env.posts) == 3


def test_the_per_address_and_per_client_limits_still_apply_and_say_the_same_thing(env, monkeypatch):
    monkeypatch.setenv("PRIVACY_INTAKE_PER_EMAIL_HOUR", "1")
    appmod._reset_rate_limits()
    assert post(env).status_code == 200
    again = post(env)
    assert again.status_code == 429 and "Retry-After" in again.headers and SUBJECT not in again.text and "address" not in again.text.lower()


def test_the_pending_backlog_is_capped_so_junk_cannot_push_real_requests_out(env, monkeypatch):
    monkeypatch.setenv("PRIVACY_PENDING_MAX", "2")
    codes = [post(env, body={"email": f"u{i}@example.com", "type": "access"}).status_code for i in range(4)]
    assert codes == [200, 200, 503, 503] and len(core._load_requests()) == 2


def test_rate_limit_memory_is_bounded_and_evicts_the_oldest_not_everything(monkeypatch):
    appmod._reset_rate_limits()
    monkeypatch.setattr(appmod, "_RL_MAX_KEYS", 100)
    monkeypatch.setattr(appmod, "RATE_BURST", 1)
    monkeypatch.setenv("PRIVACY_RATE_PER_MIN", "0.0001")
    assert appmod._take("victim", now=1.0) == 0.0 and appmod._take("victim", now=1.0) > 0         # limited
    for i in range(400):
        appmod._take(f"ip{i}", now=2.0 + i)
    assert len(appmod._BUCKETS) <= 100
    appmod._reset_rate_limits()
    monkeypatch.setenv("PRIVACY_INTAKE_PER_EMAIL_HOUR", "1")
    for i in range(400):
        appmod._email_cap(f"e{i}@example.com", now=10.0 + i)
    assert len(appmod._EMAIL_HITS) <= 100


# ── minimal retention, redacted audit ──

def test_unverified_requests_are_purged_after_the_retention_window_and_verified_ones_are_kept(env, monkeypatch):
    monkeypatch.setenv("PRIVACY_UNVERIFIED_RETENTION_HOURS", "72")
    old = time.time() - 80 * 3600
    from datetime import datetime, timezone
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()  # noqa: E731
    core._persist({"id": "DSAR-OLD", "type": "access", "email": "old@example.com", "verified": False, "status": "awaiting_verification", "created_at": iso(old), "source": "public_intake"})
    core._persist({"id": "DSAR-KEEP", "type": "access", "email": "kept@example.com", "verified": True, "status": "verified", "created_at": iso(old), "source": "public_intake"})
    core._persist({"id": "DSAR-NEW", "type": "access", "email": "new@example.com", "verified": False, "status": "awaiting_verification", "created_at": iso(time.time()), "source": "public_intake"})
    assert post(env).status_code == 200
    ids = {r["id"] for r in core._load_requests()}
    assert "DSAR-OLD" not in ids and {"DSAR-KEEP", "DSAR-NEW"} <= ids


def test_the_audit_event_is_logged_with_the_address_redacted(env):
    r = post(env, body={"email": "Jane.Doe@Example.com", "type": "delete"})
    raw = (env.tmp / "audit.jsonl").read_text()
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    ev = [x for x in rows if x.get("type") == "public_intake"]
    assert len(ev) == 1 and ev[0]["req"] == r.json()["request_id"] and ev[0]["request_type"] == "delete" and ev[0]["route"] == "POST /intake"
    assert ev[0]["email_masked"] == "j***@example.com"
    assert "jane.doe@example.com" not in raw.lower() and "Jane.Doe" not in raw
    assert core.verify_audit_chain()["intact"] is True


def test_the_record_holds_only_what_verification_and_the_deadline_need(env):
    post(env)
    rec = core._load_requests()[0]
    assert set(rec) <= {"id", "type", "email", "verified", "verify_token_hash", "verify_expires_at", "created_at", "due_at", "status", "source", "delivery"}
    assert "ip" not in json.dumps(rec).lower() and "user" not in "".join(rec) and "agent" not in "".join(rec)


# ── the route cannot be steered to do more ──

def test_request_in_the_normal_mode_only_opens_the_same_pending_record(env):
    r = post(env, "/request", {"email": SUBJECT, "type": "delete"})
    assert r.status_code == 200 and r.json()["status"] == "received" and len(env.posts) == 1
    assert env.finds == [] and env.deletes == [] and core._load_requests()[0]["status"] == "awaiting_verification"
    rid = r.json()["request_id"]
    assert env.client.post(f"/fulfil/{rid}", json={}).status_code in (401, 403, 503)         # fulfilment is a different, authenticated route
