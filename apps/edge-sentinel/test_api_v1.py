"""The /api/v1 product API: authentication in every profile, roles, sessions and CSRF, attributable decisions bound to the action digest,
persisted execution history with its read-back, the change feed and restart persistence. Real SQLite files, real operator, a fake
CrowdSec core (the same approach as test_live_cases.py)."""
from __future__ import annotations

import importlib
import json
import time

import pytest
from fastapi.testclient import TestClient

app_mod = importlib.import_module("edge-sentinel.app")
core = importlib.import_module("edge-sentinel.core")
live = importlib.import_module("edge-sentinel.live")
history_store = importlib.import_module("edge-sentinel.history_store")
identity = importlib.import_module("edge-sentinel.identity")
fixture_mode = importlib.import_module("edge-sentinel.fixture_mode")
ratelimit = importlib.import_module("edge-sentinel.ratelimit")
api_v1 = importlib.import_module("edge-sentinel.api_v1")

ADMIN = ("admin", "admin-pass-for-tests-1")
ALERT = {"scenario": "crowdsecurity/ssh-bf", "source": {"value": "93.184.216.34", "scope": "Ip"}, "created_at": "2026-10-01T10:00:00Z",
         "events_count": 12, "decisions": [{"type": "ban", "value": "93.184.216.34", "scope": "Ip", "duration": "3h59m1s", "origin": "crowdsec"}]}
IP = "93.184.216.34"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "SCRYPT_N", 2 ** 10)                      # fast hashes in tests; verification reads the stored parameters
    for k in ("PARTNERS_BASIC_AUTH_USER", "PARTNERS_BASIC_AUTH_PASS", "AGENTIC_OS_CONTROL_TOKEN", "AGENTIC_OS_APPROVAL_SECRET",
              "SENTINEL_REQUIRE_TWO_PERSON", "AGENTIC_OS_PROFILE", "AGENTIC_OS_AUTHORITY_DB", "SENTINEL_BLOCK_ALLOW_NETWORKS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_USER", ADMIN[0])
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_PASS", ADMIN[1])
    monkeypatch.setenv("SENTINEL_REQUIRE_AUTH", "1")
    monkeypatch.setenv("SENTINEL_BLOCK_ENABLED", "1")
    monkeypatch.setenv("SENTINEL_RATE_BURST", "1000000")
    monkeypatch.setenv("SENTINEL_RATE_PER_MIN", "1000000000")
    monkeypatch.setenv("OPERATOR_BASIC_AUTH_USER", "")
    monkeypatch.setenv("OPERATOR_BASIC_AUTH_PASS", "")
    live.reset_store()
    identity.reset()
    api_v1._SYNC["ts"] = 0.0
    api_v1._LOGIN_LIMITER.reset()
    bans: set = set()
    state = {"alerts": [ALERT], "verify_sees_ban": True}
    monkeypatch.setattr(core, "fetch_alerts", lambda: list(state["alerts"]))
    monkeypatch.setattr(core, "fetch_decisions", lambda: [])
    monkeypatch.setattr(core, "crowdsec_connected", lambda: True)
    monkeypatch.setattr(core, "block_ip", lambda inp: (bans.add(inp["ip"]) or {"status": "done", "ip_blocked": True, "id": f"cs-{inp['ip']}"}))
    monkeypatch.setattr(core, "unblock_ip", lambda inp: (bans.discard(inp["ip"]) or {"status": "done", "lifted": True}))
    monkeypatch.setattr(core, "triage", lambda: {"decisions": [{"value": b} for b in sorted(bans)] if state["verify_sees_ban"] else []})
    core._CACHE.update(ts=0.0, data=None)
    yield {"bans": bans, "state": state, "dir": tmp_path / "state"}
    history_store.reset()
    identity.reset()


def client() -> TestClient:
    return TestClient(app_mod.app)


def basic(c, user=ADMIN):
    c.auth = user
    return c


def make_user(admin_client, name, role, password="long-enough-password-1"):
    r = admin_client.post("/api/v1/users", json={"name": name, "password": password, "role": role})
    assert r.status_code == 201, r.text
    return (name, password)


def session(user) -> TestClient:
    c = client()
    r = c.post("/api/v1/session", json={"username": user[0], "password": user[1]})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf"]
    return c


def first_case(c):
    items = c.get("/api/v1/cases").json()["items"]
    assert items, "no case"
    return items[0]


def action_of(c, case_id):
    return c.get(f"/api/v1/cases/{case_id}").json()["actions_detail"][0]


# ── authentication in every profile ───────────────────────────────────────────────────────────────────────────

PROBE = {"/health", "/readyz"}


def _v1_paths():
    return [(m.upper(), p) for p, ops in app_mod.app.openapi()["paths"].items() if p.startswith("/api/v1/") for m in ops]


@pytest.mark.parametrize("require_auth", ["1", ""])
def test_every_v1_route_refuses_an_anonymous_request_in_every_profile(env, monkeypatch, require_auth):
    if not require_auth:
        monkeypatch.delenv("SENTINEL_REQUIRE_AUTH")
    c = client()
    for method, path in _v1_paths():
        if (method, path) == ("POST", "/api/v1/session"):
            continue                                                       # the sign-in itself: refuses a bad credential, tested below
        r = c.request(method, path.replace("{case_id}", "x").replace("{request_id}", "x").replace("{artifact_id}", "x").replace("{name}", "x").replace("{external_actor}", "x"), json={} if method != "GET" else None)
        assert r.status_code == 401, (method, path, r.status_code)
        assert r.json()["error"] == "unauthenticated"


def test_a_wrong_password_and_an_unknown_user_are_the_same_refusal(env):
    c = client()
    a = c.post("/api/v1/session", json={"username": "admin", "password": "nope-nope-nope"})
    b = c.post("/api/v1/session", json={"username": "ghost", "password": "nope-nope-nope"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_the_shipped_operator_credential_seeds_the_first_admin_and_basic_keeps_working(env):
    c = basic(client())
    r = c.get("/api/v1/session")
    assert r.status_code == 200 and r.json()["user"]["name"] == "admin" and r.json()["user"]["role"] == "admin"
    assert [u["name"] for u in c.get("/api/v1/users").json()["items"]] == ["admin"]


def test_sessions_need_the_csrf_token_and_an_honest_origin(env):
    s = session(ADMIN)
    token = s.headers.pop("X-CSRF-Token")
    assert s.post("/api/v1/cases", json={"type": "manual_block", "ip": IP}).json()["error"] == "csrf_required"
    s.headers["X-CSRF-Token"] = "wrong-token"
    assert s.post("/api/v1/cases", json={"type": "manual_block", "ip": IP}).status_code == 403
    s.headers["X-CSRF-Token"] = token
    s.headers["Origin"] = "https://evil.example"
    assert s.post("/api/v1/cases", json={"type": "manual_block", "ip": IP}).json()["error"] == "csrf_required"
    del s.headers["Origin"]
    assert s.post("/api/v1/cases", json={"type": "manual_block", "ip": IP}).status_code == 201


def test_the_session_cookie_is_httponly_samesite_strict_and_logout_ends_it(env):
    c = client()
    r = c.post("/api/v1/session", json={"username": ADMIN[0], "password": ADMIN[1]})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and cookie.startswith("sentinel_session=")
    csrf = r.json()["csrf"]
    assert c.get("/api/v1/session").status_code == 200
    assert c.delete("/api/v1/session", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert c.get("/api/v1/session").status_code == 401


def test_a_session_expires_when_idle_and_at_its_absolute_limit(env, monkeypatch):
    s = session(ADMIN)
    real = time.time
    monkeypatch.setattr(identity.time, "time", lambda: real() + identity.IDLE_SECONDS + 5)
    assert s.get("/api/v1/session").status_code == 401
    s2 = session(ADMIN)
    monkeypatch.setattr(identity.time, "time", lambda: real() + identity.ABSOLUTE_SECONDS + 5)
    assert s2.get("/api/v1/session").status_code == 401


def test_sign_in_is_rate_limited_per_user_name(env, monkeypatch):
    monkeypatch.setenv("SENTINEL_RATE_PER_MIN", "1")
    monkeypatch.setenv("SENTINEL_RATE_BURST", "2")
    api_v1._LOGIN_LIMITER.reset()
    c = client()
    codes = [c.post("/api/v1/session", json={"username": "ghost", "password": "x" * 12}).status_code for _ in range(5)]
    assert 429 in codes


# ── roles ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_role_matrix_viewer_reads_operator_stages_and_decides_admin_manages_users(env):
    adm = session(ADMIN)
    v, o = session(make_user(adm, "vera", "viewer")), session(make_user(adm, "olga", "operator"))
    assert v.get("/api/v1/cases").status_code == 200
    assert v.post("/api/v1/cases", json={"type": "manual_block", "ip": IP}).json()["error"] == "role_required"
    assert o.post("/api/v1/cases", json={"type": "manual_block", "ip": IP}).status_code == 201
    assert o.get("/api/v1/users").json()["error"] == "role_required" and v.get("/api/v1/users").status_code == 403
    assert adm.get("/api/v1/users").status_code == 200


def test_a_viewer_cannot_decide_and_a_service_credential_never_decides(env, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "service-token-for-tests-0123456789abcdef")
    adm = session(ADMIN)
    v = session(make_user(adm, "vera", "viewer"))
    case = first_case(adm)
    act = action_of(adm, case["case_id"])
    path = f"/api/v1/cases/{case['case_id']}/actions/{act['request_id']}/decision"
    assert v.post(path, json={"approved": True, "digest": act["digest"]}).json()["error"] == "role_required"
    svc = client()
    svc.headers["X-Control-Token"] = "service-token-for-tests-0123456789abcdef"
    assert svc.get("/api/v1/cases").status_code == 200                       # a service may read
    assert svc.post("/api/v1/cases", json={"type": "manual_block", "ip": IP}).status_code == 201     # and stage a proposal
    r = svc.post(path, json={"approved": True, "digest": act["digest"]})
    assert r.status_code == 403 and r.json()["error"] == "human_decision_required"
    assert svc.post(path.replace("decision", "execute"), json={"digest": act["digest"]}).json()["error"] == "human_decision_required"


def test_the_last_active_admin_cannot_be_demoted_or_disabled_and_a_disabled_user_loses_sessions(env):
    adm = session(ADMIN)
    assert adm.patch("/api/v1/users/admin", json={"role": "viewer"}).status_code == 422
    assert adm.patch("/api/v1/users/admin", json={"disabled": True}).status_code == 422
    olga = make_user(adm, "olga", "operator")
    o = session(olga)
    assert o.get("/api/v1/session").status_code == 200
    adm.patch("/api/v1/users/olga", json={"disabled": True})
    assert o.get("/api/v1/session").status_code == 401
    assert client().post("/api/v1/session", json={"username": "olga", "password": olga[1]}).status_code == 401


def test_changing_a_password_ends_the_other_sessions_and_needs_the_current_one(env):
    adm = session(ADMIN)
    olga = make_user(adm, "olga", "operator")
    a, b = session(olga), session(olga)
    assert a.post("/api/v1/session/password", json={"current": "wrong-wrong-wrong", "new": "another-long-password-2"}).status_code == 403
    assert a.post("/api/v1/session/password", json={"current": olga[1], "new": "another-long-password-2"}).status_code == 200
    assert b.get("/api/v1/session").status_code == 401 and a.get("/api/v1/session").status_code == 200
    assert client().post("/api/v1/session", json={"username": "olga", "password": "another-long-password-2"}).status_code == 200


def test_passwords_have_a_minimum_and_are_stored_as_scrypt_hashes(env):
    adm = session(ADMIN)
    assert adm.post("/api/v1/users", json={"name": "short", "password": "tooshort", "role": "viewer"}).status_code == 422
    make_user(adm, "olga", "operator")
    stored = history_store.get().one("SELECT password_hash FROM users WHERE name='olga'")["password_hash"]
    assert stored.startswith("scrypt$") and "long-enough" not in stored


# ── the decision workflow, attributable and read back ────────────────────────────────────────────────────────

def decide(c, case, act, approved=True, digest=None, **kw):
    return c.post(f"/api/v1/cases/{case['case_id']}/actions/{act['request_id']}/decision", json={"approved": approved, "digest": digest or act["digest"], **kw})


def run(c, case, act):
    return c.post(f"/api/v1/cases/{case['case_id']}/actions/{act['request_id']}/execute", json={"digest": act["digest"]})


def test_an_alert_becomes_a_case_an_operator_approves_and_executes_and_the_read_back_is_persisted(env):
    adm = session(ADMIN)
    olga = session(make_user(adm, "olga", "operator"))
    case = first_case(olga)
    assert case["status"] == "AWAITING_APPROVAL" and case["open_action"]["capability"] == "sentinel.block_ip"
    act = action_of(olga, case["case_id"])
    assert act["state"] == "AWAITING_APPROVAL" and len(act["digest"]) == 64
    d = decide(olga, case, act)
    assert d.status_code == 200 and d.json()["awaiting_execution"] is True
    assert d.json()["decision"]["actor"] == "user:olga" and d.json()["decision"]["via"] == "session" and d.json()["decision"]["role"] == "operator"
    ex = run(olga, case, act)
    assert ex.status_code == 200, ex.text
    out = ex.json()
    assert out["executed"] is True and out["dry_run"] is False and out["receipt"]["status"] == "SUCCEEDED"
    assert out["verification"]["state"] == "verified" and IP in out["verification"]["detail"] and out["case_status"] == "CONTAINED"
    assert IP in env["bans"]
    a = olga.get(f"/api/v1/actions/{act['request_id']}").json()
    assert a["state"] == "EXECUTED" and a["verification"]["state"] == "verified" and a["decision"]["actor"] == "user:olga"
    assert olga.get(f"/api/v1/actions/{act['request_id']}/verification").json()["verification"]["state"] == "verified"
    types = [i["type"] for i in olga.get("/api/v1/changes").json()["items"]]
    for t in ("case.opened", "action.proposed", "action.decided", "action.executed", "action.verified"):
        assert t in types, types


def test_an_unverified_ban_is_not_contained_and_says_so(env):
    env["state"]["verify_sees_ban"] = False                       # the call succeeds but the read-back does not find the ban
    adm = session(ADMIN)
    case = first_case(adm)
    act = action_of(adm, case["case_id"])
    decide(adm, case, act)
    out = run(adm, case, act).json()
    assert out["receipt"]["status"] == "SUCCEEDED" and out["verification"]["state"] == "unverified"
    assert out["case_status"] != "CONTAINED"


def test_a_dry_run_is_labelled_everywhere_and_touches_nothing(env, monkeypatch):
    monkeypatch.delenv("SENTINEL_BLOCK_ENABLED")
    adm = session(ADMIN)
    assert adm.get("/api/v1/info").json()["mode"]["dry_run"] is True
    case = first_case(adm)
    act = action_of(adm, case["case_id"])
    decide(adm, case, act)
    out = run(adm, case, act).json()
    assert out["dry_run"] is True and out["executed"] is False and out["receipt"]["status"] == "DRY_RUN" and out["verification"]["state"] == "dry_run"
    assert env["bans"] == set()
    assert adm.get(f"/api/v1/actions/{act['request_id']}").json()["receipt"]["dry_run"] is True


def test_an_approval_is_bound_to_the_digest_it_was_shown(env):
    adm = session(ADMIN)
    case = first_case(adm)
    act = action_of(adm, case["case_id"])
    r = decide(adm, case, act, digest="0" * 64)
    assert r.status_code == 409 and r.json()["error"] == "stale_approval"
    assert adm.post(f"/api/v1/cases/{case['case_id']}/actions/{act['request_id']}/decision", json={"approved": True}).json()["error"] == "digest_required"
    assert adm.get(f"/api/v1/actions/{act['request_id']}").json()["state"] == "AWAITING_APPROVAL"
    decide(adm, case, act)
    assert adm.post(f"/api/v1/cases/{case['case_id']}/actions/{act['request_id']}/execute", json={"digest": "f" * 64}).json()["error"] == "stale_approval"


def test_execution_needs_an_approving_decision_and_a_rejection_ends_it(env):
    adm = session(ADMIN)
    case = first_case(adm)
    act = action_of(adm, case["case_id"])
    assert run(adm, case, act).json()["error"] == "conflict"
    assert decide(adm, case, act, approved=False, rationale="known scanner").status_code == 200
    a = adm.get(f"/api/v1/actions/{act['request_id']}").json()
    assert a["state"] == "REJECTED" and a["decision"]["approved"] is False and a["decision"]["rationale"] == "known scanner"
    assert run(adm, case, act).status_code == 409 and env["bans"] == set()


def test_the_two_person_rule_when_enabled(env, monkeypatch):
    monkeypatch.setenv("SENTINEL_REQUIRE_TWO_PERSON", "1")
    adm = session(ADMIN)
    olga, omar = session(make_user(adm, "olga", "operator")), session(make_user(adm, "omar", "operator"))
    case = first_case(adm)
    act = action_of(adm, case["case_id"])
    decide(olga, case, act)
    r = run(olga, case, act)
    assert r.status_code == 409 and r.json()["error"] == "two_person_required"
    assert run(omar, case, act).json()["executed"] is True
    assert omar.get(f"/api/v1/actions/{act['request_id']}").json()["decision"]["actor"] == "user:olga"


def test_the_body_never_names_the_actor(env):
    adm = session(ADMIN)
    case = first_case(adm)
    act = action_of(adm, case["case_id"])
    r = decide(adm, case, act, actor="user:somebody-else", via="runtime", _approval=True)
    assert r.json()["decision"]["actor"] == "user:admin" and r.json()["decision"]["via"] == "session"


def test_the_ban_guard_still_refuses_protected_addresses_when_staging(env):
    adm = session(ADMIN)
    for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "not-an-ip"):
        r = adm.post("/api/v1/cases", json={"type": "manual_block", "ip": ip})
        assert r.status_code == 422 and r.json()["error"] == "refused", (ip, r.text)
    assert adm.post("/api/v1/cases", json={"type": "other", "ip": IP}).json()["error"] == "invalid_type"


# ── persistence ──────────────────────────────────────────────────────────────────────────────────────────────

def _restart(env):
    live.reset_store()
    identity.reset()
    api_v1._SYNC["ts"] = 0.0
    core._CACHE.update(ts=0.0, data=None)


def test_cases_decisions_receipts_and_history_survive_a_restart_and_an_expired_alert(env):
    adm = session(ADMIN)
    case = first_case(adm)
    act = action_of(adm, case["case_id"])
    decide(adm, case, act)
    run(adm, case, act)
    _restart(env)
    env["state"]["alerts"] = []                                              # CrowdSec has forgotten the alert
    s = session(ADMIN)                                                        # the user table survived too
    got = s.get(f"/api/v1/cases/{case['case_id']}").json()
    assert got["summary"]["status"] == "CONTAINED" and got["active"] is False
    a = s.get(f"/api/v1/actions/{act['request_id']}").json()
    assert a["state"] == "EXECUTED" and a["decision"]["actor"] == "user:admin" and a["verification"]["state"] == "verified" and a["receipt"]["status"] == "SUCCEEDED"
    assert s.get("/api/v1/changes").json()["head"] >= 5
    assert decide(s, case, act).json()["error"] == "case_not_active"


def test_evidence_is_content_addressed_downloadable_and_scoped_to_its_case(env):
    adm = session(ADMIN)
    case = first_case(adm)
    got = adm.get(f"/api/v1/cases/{case['case_id']}").json()
    eid = got["evidence"][0]["artifact_id"]
    r = adm.get(f"/api/v1/cases/{case['case_id']}/evidence/{eid}")
    import hashlib
    assert r.status_code == 200 and r.headers["x-content-sha256"] == hashlib.sha256(r.content).hexdigest() and "attachment" in r.headers["content-disposition"]
    assert json.loads(r.content)["scenario"] == "crowdsecurity/ssh-bf" and "duration" not in json.dumps(json.loads(r.content)["decisions"])
    assert adm.get(f"/api/v1/cases/{case['case_id']}/evidence/ev-0000000000000000").status_code == 404
    assert got["replay"]["deterministic"] is True


def test_evidence_rows_refuse_to_change_bytes(env):
    adm = session(ADMIN)
    case = first_case(adm)
    eid = adm.get(f"/api/v1/cases/{case['case_id']}").json()["evidence"][0]["artifact_id"]
    hs = history_store.get()
    hs.run("UPDATE evidence SET content = ? WHERE artifact_id = ?", ("{}", eid))              # a tamper, directly in the file
    assert adm.get(f"/api/v1/cases/{case['case_id']}/evidence/{eid}").status_code == 500       # served only if it still matches its digest
    live.reset_store()
    with pytest.raises(history_store.IntegrityError):
        live.sync_cases()


def test_a_manual_case_is_rebuilt_after_a_restart_from_its_stored_request(env):
    adm = session(ADMIN)
    env["state"]["alerts"] = []
    r = adm.post("/api/v1/cases", json={"type": "manual_block", "ip": "198.18.0.1", "reason": "x"})
    assert r.status_code == 422                                                # benchmarking space is reserved: refused like any non-public target
    r = adm.post("/api/v1/cases", json={"type": "manual_block", "ip": "8.8.4.4", "reason": "seen scanning", "duration": "2h"})
    assert r.status_code == 201 and r.json()["state"] == "AWAITING_APPROVAL"
    cid, rid = r.json()["case_id"], r.json()["request_id"]
    _restart(env)
    s = session(ADMIN)
    assert [c["case_id"] for c in s.get("/api/v1/cases").json()["items"]] == [cid]
    act = s.get(f"/api/v1/actions/{rid}").json()
    assert act["params"]["ip"] == "8.8.4.4" and act["params"]["duration"] == "2h"
    assert s.post(f"/api/v1/cases/{cid}/actions/{rid}/decision", json={"approved": True, "digest": act["digest"]}).status_code == 200
    assert s.post(f"/api/v1/cases/{cid}/actions/{rid}/execute", json={"digest": act["digest"]}).json()["verification"]["state"] == "verified"
    assert "8.8.4.4" in env["bans"]


def test_a_manual_unblock_is_verified_by_the_absence_of_the_ban(env):
    adm = session(ADMIN)
    env["bans"].add("8.8.4.4")
    r = adm.post("/api/v1/cases", json={"type": "manual_unblock", "ip": "8.8.4.4", "reason": "false positive"}).json()
    act = adm.get(f"/api/v1/actions/{r['request_id']}").json()
    assert act["capability"] == "sentinel.unblock_ip"
    adm.post(f"/api/v1/cases/{r['case_id']}/actions/{r['request_id']}/decision", json={"approved": True, "digest": act["digest"]})
    out = adm.post(f"/api/v1/cases/{r['case_id']}/actions/{r['request_id']}/execute", json={"digest": act["digest"]}).json()
    assert out["verification"]["state"] == "verified" and "no longer present" in out["verification"]["detail"] and "8.8.4.4" not in env["bans"]


def test_an_unusable_history_store_refuses_decisions_instead_of_continuing_in_memory(env, monkeypatch):
    adm = session(ADMIN)
    case = first_case(adm)
    act = action_of(adm, case["case_id"])
    monkeypatch.setattr(history_store.HistoryStore, "health", lambda self: {"ok": False, "persistent": True, "error": "disk full"})
    r = decide(adm, case, act)
    assert r.status_code == 409 and "unavailable" in r.json()["message"]
    assert live.store().actions[act["request_id"]].state.value == "AWAITING_APPROVAL"


# ── approval tokens (Projects) ───────────────────────────────────────────────────────────────────────────────

def _tok(op, cap, inputs, by):
    from agentic_os.mission.approval import mint_approval_token
    return mint_approval_token(operator=op, capability=cap, mission_id=None, node_id=None, inputs=inputs, marker={"approved": True, "by": by, "via": "projects"}, ttl_s=120)


def test_an_approval_token_for_a_linked_user_decides_once_and_is_attributed_to_that_user(env, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", "approval-secret-for-tests-0123456789abcdef")
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "service-token-for-tests-0123456789abcdef")
    monkeypatch.setattr(api_v1, "_REPLAY", None)
    adm = session(ADMIN)
    make_user(adm, "olga", "operator")
    case = first_case(adm)
    act = action_of(adm, case["case_id"])
    inputs = {"case_id": case["case_id"], "request_id": act["request_id"], "approved": True, "digest": act["digest"]}
    svc = client()
    svc.headers["X-Control-Token"] = "service-token-for-tests-0123456789abcdef"
    path = f"/api/v1/cases/{case['case_id']}/actions/{act['request_id']}/decision"
    body = {"approved": True, "digest": act["digest"]}
    tok = _tok("edge-sentinel", "sentinel.decide", inputs, "projects:local-owner")
    r = svc.post(path, json=body, headers={"X-Approval-Token": tok})
    assert r.status_code == 403 and r.json()["error"] == "actor_not_linked"           # the approval stays consumed? no: it was not accepted as a decision
    assert adm.post("/api/v1/actor-links", json={"external_actor": "projects:local-owner", "user": "olga"}).status_code == 201
    tok2 = _tok("edge-sentinel", "sentinel.decide", inputs, "projects:local-owner")
    ok = svc.post(path, json=body, headers={"X-Approval-Token": tok2})
    assert ok.status_code == 200, ok.text
    assert ok.json()["decision"]["actor"] == "user:olga" and ok.json()["decision"]["via"] == "projects" and ok.json()["decision"]["external_actor"] == "projects:local-owner"
    assert svc.post(path, json=body, headers={"X-Approval-Token": tok2}).json()["error"] == "approval_token_invalid"       # replay
    wrong = _tok("edge-sentinel", "sentinel.decide", {**inputs, "digest": "a" * 64}, "projects:local-owner")
    assert svc.post(path, json=body, headers={"X-Approval-Token": wrong}).json()["error"] == "approval_token_invalid"       # bound to other content
    assert adm.delete("/api/v1/actor-links/projects:local-owner").status_code == 200


def test_a_linked_viewer_cannot_decide_through_projects(env, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", "approval-secret-for-tests-0123456789abcdef")
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "service-token-for-tests-0123456789abcdef")
    monkeypatch.setattr(api_v1, "_REPLAY", None)
    adm = session(ADMIN)
    make_user(adm, "vera", "viewer")
    adm.post("/api/v1/actor-links", json={"external_actor": "projects:v", "user": "vera"})
    case = first_case(adm)
    act = action_of(adm, case["case_id"])
    inputs = {"case_id": case["case_id"], "request_id": act["request_id"], "approved": True, "digest": act["digest"]}
    svc = client()
    svc.headers["X-Control-Token"] = "service-token-for-tests-0123456789abcdef"
    r = svc.post(f"/api/v1/cases/{case['case_id']}/actions/{act['request_id']}/decision", json={"approved": True, "digest": act["digest"]},
                 headers={"X-Approval-Token": _tok("edge-sentinel", "sentinel.decide", inputs, "projects:v")})
    assert r.status_code == 403 and r.json()["error"] == "role_required"


# ── reads ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_info_posture_events_decisions_and_pagination(env):
    s = session(ADMIN)
    info = s.get("/api/v1/info").json()
    assert info["product"] == "edge-sentinel" and info["tenant_label"] == "Edge Sentinel" and info["store"]["persistent"] is True
    assert {u["feature"] for u in info["unavailable"]} >= {"dfir", "enrichment", "risk_bridge"}
    assert all({"feature", "code", "reason"} <= set(u) for u in info["unavailable"])
    ev = s.get("/api/v1/events").json()
    assert ev["items"][0]["source_ip"] == IP and ev["items"][0]["severity"] == "critical"
    s.get("/api/v1/cases")
    assert s.get("/api/v1/events").json()["items"][0]["case_id"].startswith("case-")
    assert s.get("/api/v1/events?severity=low").json()["items"] == []
    assert s.get("/api/v1/posture").json()["source"] == "live"
    assert s.get("/api/v1/cases?limit=1&cursor=!!!").status_code == 400
    env["state"]["alerts"] = [{**ALERT, "created_at": f"2026-10-0{i}T10:00:00Z", "source": {"value": f"8.8.8.{i}", "scope": "Ip"}} for i in range(1, 6)]
    api_v1._SYNC["ts"] = 0.0
    page1 = s.get("/api/v1/cases?limit=2").json()
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    page2 = s.get(f"/api/v1/cases?limit=10&cursor={page1['next_cursor']}").json()
    ids = [c["case_id"] for c in page1["items"] + page2["items"]]
    assert len(ids) == len(set(ids)) >= 5


def test_the_demo_fixture_is_labelled_and_executes_nothing(env, monkeypatch):
    monkeypatch.setenv("EDGE_SENTINEL_DEMO", "1")
    fixture_mode.install()
    try:
        s = session(ADMIN)
        info = s.get("/api/v1/info").json()
        assert info["mode"]["demo"] is True and any(u["code"] == "DEMO_MODE" for u in info["unavailable"])
        assert all(e["origin"] == "fixture" for e in s.get("/api/v1/events").json()["items"])
    finally:
        fixture_mode.uninstall()


def test_the_users_cli_creates_lists_and_disables_users_in_the_same_store_the_server_uses(env, tmp_path, capsys):
    cli = importlib.import_module("edge-sentinel.cli")
    pwf = tmp_path / "pw"
    pwf.write_text("a-long-enough-password-9\n")
    assert cli.main(["users", "add", "carla", "--role", "operator", "--password-file", str(pwf)]) == 0
    assert cli.main(["users", "add", "carla", "--role", "operator", "--password-file", str(pwf)]) == 1          # exists
    assert cli.main(["users", "list"]) == 0 and "carla" in capsys.readouterr().out
    s = session(("carla", "a-long-enough-password-9"))
    assert s.get("/api/v1/session").json()["user"]["role"] == "operator"
    assert cli.main(["users", "disable", "carla"]) == 0
    assert s.get("/api/v1/session").status_code == 401


# ── the Mission Runtime path writes the same records ─────────────────────────────────────────────────────────

def test_a_runtime_approved_block_goes_through_the_same_case_decision_receipt_and_read_back(env):
    op = importlib.import_module("edge-sentinel.operator").build_edge_sentinel_operator()
    staged = op.invoke("sentinel.block_ip", {"ip": "8.8.4.4"})
    assert staged["status"] == "pending_approval" and staged["request_id"].startswith("act-")
    s = session(ADMIN)
    assert s.get(f"/api/v1/actions/{staged['request_id']}").json()["state"] == "AWAITING_APPROVAL"       # visible in the queue before anyone approves
    res = op.invoke("sentinel.block_ip", {"ip": "8.8.4.4", "_approval": {"approved": True, "by": "projects:local-owner"}})
    assert res["ip_blocked"] is True and res["request_id"] == staged["request_id"] and res["receipt_id"].startswith("rcpt-")
    a = s.get(f"/api/v1/actions/{res['request_id']}").json()
    assert a["state"] == "EXECUTED" and a["decision"]["via"] == "runtime" and a["decision"]["actor"] == "runtime:projects:local-owner"
    assert a["verification"]["state"] == "verified" and "8.8.4.4" in env["bans"]
    undo = op.invoke("sentinel.unblock_ip", {"ip": "8.8.4.4", "_approval": {"approved": True, "by": "projects:local-owner"}})
    assert undo["ip_unblocked"] is True and "8.8.4.4" not in env["bans"]


def test_a_runtime_call_for_a_protected_address_is_refused_before_anything_is_recorded(env):
    op = importlib.import_module("edge-sentinel.operator").build_edge_sentinel_operator()
    res = op.invoke("sentinel.block_ip", {"ip": "10.0.0.1", "_approval": {"approved": True, "by": "x"}})
    assert res["status"] == "error" and res["ip_blocked"] is False and env["bans"] == set()
