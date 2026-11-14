"""The standalone product profile: file secrets, the CrowdSec watcher path, the require-auth gate and the readiness probe.

Three kinds of test, none needing docker or a real CrowdSec:
* a REAL local HTTP server speaks the CrowdSec LAPI (watcher login, alerts, bans; a bouncer key may read decisions
  but not write them, as the real LAPI behaves), so the watcher code is exercised over a socket;
* the require-auth gate is exercised in process with TestClient;
* a frozen contract of routes and environment variables proves the reference (rAIlsApps) behaviour is unchanged.
"""
from __future__ import annotations

import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

app_mod = importlib.import_module("edge-sentinel.app")
core = importlib.import_module("edge-sentinel.core")
secret_files = importlib.import_module("edge-sentinel.secret_files")
security = importlib.import_module("edge-sentinel.security")
auth = importlib.import_module("edge-sentinel.auth")

BOUNCER = "bouncer-key-for-tests"
MACHINE, PASSWORD = "sentinel-reader", "machine-password-for-tests"
JWT = "jwt-for-tests"


class _FakeLapi(BaseHTTPRequestHandler):
    state = {"alerts": [], "bans": [], "logins": 0, "expire_next_token": False}

    def log_message(self, *a):  # silence
        pass

    def _send(self, code, body=None):
        raw = json.dumps(body).encode() if (body is not None or code == 200) else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"null")

    def _watcher(self):
        return self.headers.get("Authorization") == f"Bearer {JWT}" and not self.state["expire_next_token"]

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/watchers/login":
            b = self._body()
            if b.get("machine_id") == MACHINE and b.get("password") == PASSWORD:
                self.state["logins"] += 1
                self.state["expire_next_token"] = False
                return self._send(200, {"code": 200, "token": JWT, "expire": "2999-01-01T00:00:00Z"})
            return self._send(401, {"message": "bad login"})
        if path == "/v1/alerts":
            if not self._watcher():
                return self._send(401, {"message": "cookie token is empty"})
            for alert in self._body():
                self.state["alerts"].append(alert)
                for d in alert.get("decisions", []):
                    self.state["bans"].append({"value": d["value"], "scope": d["scope"], "type": d["type"],
                                               "duration": d["duration"], "origin": d["origin"], "scenario": d["scenario"]})
            return self._send(201, ["1"])
        if path == "/v1/decisions":
            return self._send(405)                     # the real LAPI: decisions are written as alerts
        return self._send(404)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/v1/alerts":
            if not self._watcher():
                return self._send(401, {"message": "cookie token is empty"})
            return self._send(200, self.state["alerts"] or None)
        if u.path == "/v1/decisions":
            if self.headers.get("X-Api-Key") != BOUNCER:
                return self._send(403)
            ip = parse_qs(u.query).get("ip", [None])[0]
            rows = [b for b in self.state["bans"] if not ip or b["value"] == ip]
            return self._send(200, rows or None)
        return self._send(404)

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path == "/v1/decisions":
            if not self._watcher():
                return self._send(401, {"message": "cookie token is empty"})
            ip = parse_qs(u.query).get("ip", [None])[0]
            n = len([b for b in self.state["bans"] if b["value"] == ip])
            self.state["bans"] = [b for b in self.state["bans"] if b["value"] != ip]
            return self._send(200, {"nbDeleted": str(n)})
        return self._send(404)


@pytest.fixture()
def lapi(monkeypatch):
    _FakeLapi.state = {"alerts": [], "bans": [], "logins": 0, "expire_next_token": False}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLapi)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setattr(core, "CROWDSEC_LAPI_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setattr(core, "CROWDSEC_BOUNCER_KEY", BOUNCER)
    monkeypatch.setattr(core, "CROWDSEC_MACHINE_ID", MACHINE)
    monkeypatch.setattr(core, "CROWDSEC_MACHINE_PASSWORD", PASSWORD)
    core._JWT.update(token="", exp=0.0)
    monkeypatch.setenv("SENTINEL_BLOCK_ALLOW_NETWORKS", "")
    yield _FakeLapi.state
    srv.shutdown()


# ── file secrets ───────────────────────────────────────────────────────────────

def test_a_secret_file_fills_an_unset_variable_and_never_overrides_one_that_is_set(tmp_path):
    f = tmp_path / "pw"
    f.write_text("from-file\n")
    env = {"SENTINEL_BASIC_AUTH_PASS_FILE": str(f)}
    assert secret_files.load_file_secrets(env) == ["SENTINEL_BASIC_AUTH_PASS"]
    assert env["SENTINEL_BASIC_AUTH_PASS"] == "from-file"
    env2 = {"SENTINEL_BASIC_AUTH_PASS_FILE": str(f), "SENTINEL_BASIC_AUTH_PASS": "explicit"}
    assert secret_files.load_file_secrets(env2) == [] and env2["SENTINEL_BASIC_AUTH_PASS"] == "explicit"


def test_an_unreadable_or_empty_secret_file_stops_the_start_and_never_prints_a_value(tmp_path):
    with pytest.raises(secret_files.SecretFileError):
        secret_files.load_file_secrets({"CROWDSEC_BOUNCER_KEY_FILE": str(tmp_path / "missing")})
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    with pytest.raises(secret_files.SecretFileError) as e:
        secret_files.load_file_secrets({"CROWDSEC_MACHINE_PASSWORD_FILE": str(empty)})
    assert "CROWDSEC_MACHINE_PASSWORD" in str(e.value)


# ── the watcher path against a LAPI that behaves like the real one ───────────────

def test_alerts_are_read_over_the_lapi_with_the_watcher_login_and_no_docker(lapi, monkeypatch):
    lapi["alerts"].append({"scenario": "crowdsecurity/ssh-bf", "source": {"value": "203.0.113.9", "scope": "Ip"}})
    monkeypatch.setattr(core, "_cscli", lambda a: (_ for _ in ()).throw(AssertionError("docker/cscli must not be used")))
    alerts = core.fetch_alerts()
    assert [a["scenario"] for a in alerts] == ["crowdsecurity/ssh-bf"]
    assert core.alerts_status()["ok"] is True and lapi["logins"] == 1
    core.fetch_alerts()
    assert lapi["logins"] == 1, "the JWT is cached, not re-requested per read"


def test_a_wrong_machine_password_is_reported_as_an_unavailable_feed_not_as_no_alerts(lapi, monkeypatch):
    monkeypatch.setattr(core, "CROWDSEC_MACHINE_PASSWORD", "wrong")
    assert core.fetch_alerts() == []
    st = core.alerts_status()
    assert st["ok"] is False and "failed" in st["note"] and "wrong" not in st["note"]


def test_an_expired_token_is_replaced_once(lapi):
    core.fetch_alerts()
    lapi["expire_next_token"] = True        # the LAPI now refuses the cached JWT (the next login clears this)
    core.fetch_alerts()
    assert core.alerts_status() == {"ok": True, "note": ""} and lapi["logins"] == 2


def test_a_ban_is_written_over_the_lapi_and_verified_by_read_back_then_lifted(lapi):
    out = core.block_ip({"ip": "93.184.216.34", "duration": "2h", "reason": "test ban"})
    assert out["status"] == "done" and out["enforced"] is True and out["lapi_status"] == 201, out
    assert lapi["alerts"][0]["decisions"][0]["value"] == "93.184.216.34"
    assert [b["value"] for b in lapi["bans"]] == ["93.184.216.34"]
    undo = core.unblock_ip({"ip": "93.184.216.34"})
    assert undo["status"] == "done" and undo["lifted"] is True and lapi["bans"] == []


def test_without_a_watcher_login_the_reference_bouncer_write_path_is_unchanged(lapi, monkeypatch):
    monkeypatch.setattr(core, "CROWDSEC_MACHINE_ID", "")
    out = core.block_ip({"ip": "93.184.216.35"})
    assert out["lapi_status"] == 405 and out["enforced"] is False     # what the real LAPI does with the bouncer key


# ── the require-auth gate ─────────────────────────────────────────────────────

CRED = ("operator", "s3cret-for-tests")


@pytest.fixture()
def gated(monkeypatch, tmp_path):
    for k in ("PARTNERS_BASIC_AUTH_USER", "PARTNERS_BASIC_AUTH_PASS", "SENTINEL_STATE_DIR", "AGENTIC_OS_PROFILE",
              "AGENTIC_OS_AUTHORITY_DB", "AGENTIC_OS_CONTROL_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SENTINEL_REQUIRE_AUTH", "1")
    monkeypatch.setenv("OPERATOR_BASIC_AUTH_USER", "")        # the gate copies the credential here; the teardown restores
    monkeypatch.setenv("OPERATOR_BASIC_AUTH_PASS", "")
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_USER", CRED[0])
    monkeypatch.setenv("SENTINEL_BASIC_AUTH_PASS", CRED[1])
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(core, "fetch_decisions", lambda: [])
    monkeypatch.setattr(core, "fetch_alerts", lambda: [])
    monkeypatch.setattr(core, "crowdsec_connected", lambda: False)
    core._CACHE.update(ts=0.0, data=None)
    return TestClient(app_mod.app)


GET_ROUTES = ["/", "/api/activity", "/api/cases", "/api/cases/x", "/api/permissions/ui", "/api/network/review",
              "/api/network/ui", "/api/backups/status", "/api/backups/ui", "/capabilities", "/_security", "/openapi.json", "/docs"]
POST_ROUTES = ["/agent/run", "/api/agent", "/api/permissions/query", "/api/cases/x/respond", "/api/cases/x/execute"]


def test_every_non_probe_route_refuses_an_unauthenticated_request(gated):
    for path in GET_ROUTES:
        r = gated.get(path)
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic"), path
    for path in POST_ROUTES:
        r = gated.post(path, json={})
        assert r.status_code == 401, path
    assert gated.get("/", auth=("operator", "wrong")).status_code == 401
    assert gated.get("/", auth=("wrong", CRED[1])).status_code == 401


def test_the_refusal_still_carries_the_security_headers(gated):
    r = gated.get("/api/cases")
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]


def test_the_two_probes_stay_public_and_leak_no_case_data(gated):
    assert gated.get("/health").status_code == 200
    r = gated.get("/readyz")
    assert r.status_code == 200 and r.json()["ready"] is True and r.json()["auth_required"] is True
    assert set(r.json()) == {"ready", "checks", "profile", "auth_required", "crowdsec_connected"}


def test_an_authenticated_operator_is_served(gated):
    for path in ("/", "/api/activity", "/api/cases", "/capabilities"):
        assert gated.get(path, auth=CRED).status_code == 200, path


def test_invoke_is_not_opened_by_the_gate_and_is_refused_without_the_control_token(gated, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", raising=False)   # the apps' conftest sets it for the other suites
    r = gated.post("/invoke", json={"capability": "sentinel.triage", "inputs": {}})
    assert r.status_code == 503 and "AGENTIC_OS_CONTROL_TOKEN" in r.text     # the operator's own gate, not a Basic prompt


def test_authorization_a_privileged_action_needs_an_approved_decision_even_for_the_operator(gated):
    r = gated.post("/api/cases/nope/execute", json={"request_id": "r"}, auth=CRED)
    assert r.status_code == 404                      # authenticated, but there is no such case/request: nothing runs
    r = gated.post("/agent/run", json={"action": "approve_block", "ip": "203.0.113.5"}, auth=CRED)
    assert r.status_code == 200 and r.json().get("status") in ("dry_run", "error", "done"), r.text
    assert r.json().get("status") != "done"          # SENTINEL_BLOCK_ENABLED is off: a dry run, never a ban


ALERT = {"scenario": "crowdsecurity/ssh-bf", "source": {"value": "93.184.216.34", "scope": "Ip"},
         "created_at": "2026-10-01T10:00:00Z", "events_count": 12}


def test_the_operator_signs_in_once_and_the_decision_then_execution_workflow_runs_end_to_end(gated, monkeypatch):
    """alert -> case -> (anonymous: refused) -> approve -> execute (a dry run unless SENTINEL_BLOCK_ENABLED) -> receipt; the decision is persisted."""
    live = importlib.import_module("edge-sentinel.live")
    live.reset_store()
    monkeypatch.setattr(core, "fetch_alerts", lambda: [ALERT])
    cases = gated.get("/api/cases", auth=CRED).json()["cases"]
    assert len(cases) == 1 and cases[0]["status"] == "AWAITING_APPROVAL"
    cid, rid = cases[0]["case_id"], cases[0]["open_action"]["request_id"]
    assert gated.post(f"/api/cases/{cid}/respond", json={"request_id": rid, "approved": True}).status_code == 401
    ok = gated.post(f"/api/cases/{cid}/respond", json={"request_id": rid, "approved": True}, auth=CRED)
    assert ok.status_code == 200 and ok.json()["awaiting_execution"] is True and ok.json()["decision"]["actor"] == "operator:operator", ok.text
    ex = gated.post(f"/api/cases/{cid}/execute", json={"request_id": rid}, auth=CRED)
    assert ex.status_code == 200 and ex.json()["dry_run"] is True and ex.json()["receipt"]["status"] == "DRY_RUN", ex.text
    state = json.loads((__import__("pathlib").Path(__import__("os").environ["SENTINEL_STATE_DIR"]) / "cases-state.json").read_text())
    assert list(state["decisions"].values())[0]["actor"] == "operator:operator"


def test_start_is_refused_when_auth_is_required_and_no_credential_exists(monkeypatch):
    monkeypatch.setenv("SENTINEL_REQUIRE_AUTH", "1")
    for k in ("SENTINEL_BASIC_AUTH_USER", "SENTINEL_BASIC_AUTH_PASS", "PARTNERS_BASIC_AUTH_USER", "PARTNERS_BASIC_AUTH_PASS"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(security.ExposureRefused):
        security.auth_required_guard()
    with pytest.raises(security.ExposureRefused):
        with TestClient(app_mod.app):          # the startup hook runs inside the lifespan
            pass


def test_the_development_path_is_explicit_and_unchanged(monkeypatch):
    monkeypatch.delenv("SENTINEL_REQUIRE_AUTH", raising=False)
    monkeypatch.setattr(core, "fetch_decisions", lambda: [])
    monkeypatch.setattr(core, "fetch_alerts", lambda: [])
    core._CACHE.update(ts=0.0, data=None)
    c = TestClient(app_mod.app)
    assert c.get("/api/activity").status_code == 200       # reference behaviour: reads open on loopback / behind the suite
    assert c.post("/api/cases/nope/execute", json={"request_id": "r"}).status_code in (401, 503)   # execution always needs the credential


# ── readiness ─────────────────────────────────────────────────────────────────

def test_readiness_fails_when_the_state_directory_is_unusable(gated, monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(blocker / "sub"))     # a path under a regular file cannot be created
    r = gated.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["state"] is False


def test_readiness_in_the_production_profile_needs_an_authority_store(gated, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    assert gated.get("/readyz").status_code == 503
    monkeypatch.setenv("AGENTIC_OS_AUTHORITY_DB", str(tmp_path / "authority.db"))
    assert gated.get("/readyz").status_code == 200


# ── the reference (rAIlsApps) contract is unchanged ────────────────────────────

REFERENCE_ROUTES = {
    ("GET", "/health"), ("GET", "/api/activity"), ("GET", "/"), ("GET", "/api/cases"), ("GET", "/api/cases/{case_id}"),
    ("POST", "/api/cases/{case_id}/respond"), ("POST", "/api/cases/{case_id}/execute"), ("POST", "/agent/run"),
    ("POST", "/api/agent"), ("POST", "/api/permissions/query"), ("GET", "/api/permissions/ui"),
    ("GET", "/api/network/review"), ("GET", "/api/network/ui"), ("GET", "/api/backups/status"), ("GET", "/api/backups/ui"),
    ("GET", "/capabilities"), ("POST", "/invoke"), ("GET", "/_security"),
}
REFERENCE_ENV = {
    "CROWDSEC_LAPI_URL", "CROWDSEC_BOUNCER_KEY", "CROWDSEC_CONTAINER", "CROWDSEC_FRONT_URL", "PORT", "SENTINEL_BIND_HOST",
    "SENTINEL_ALLOW_EXPOSURE", "SENTINEL_ALLOW_UNAUTHENTICATED_EXPOSURE", "SENTINEL_BASIC_AUTH_USER", "SENTINEL_BASIC_AUTH_PASS",
    "PARTNERS_BASIC_AUTH_USER", "PARTNERS_BASIC_AUTH_PASS", "SENTINEL_BLOCK_ENABLED", "EDGE_SENTINEL_DEMO", "EDGE_SENTINEL_DEMO_SCENARIO",
    "SENTINEL_PROTECTED_IPS", "SENTINEL_HOST_IPS", "SENTINEL_BLOCK_ALLOW_NETWORKS", "SENTINEL_BLOCK_ALLOW_WIDE_CIDR",
    "SENTINEL_BLOCK_MAX_HOURS", "SENTINEL_STATE_DIR", "WAZUH_TLS_VERIFY", "DOCKER_CMD",
}


def test_every_reference_route_still_exists_and_new_ones_are_only_the_readiness_probe():
    have = {(m.upper(), p) for p, ops in app_mod.app.openapi()["paths"].items() for m in ops if m.upper() in ("GET", "POST")}
    docs = {("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/docs/oauth2-redirect"), ("GET", "/redoc")}
    assert REFERENCE_ROUTES <= have, REFERENCE_ROUTES - have
    assert have - REFERENCE_ROUTES - docs == {("GET", "/readyz")}


def test_every_reference_variable_is_still_documented_in_the_configuration_contract():
    from pathlib import Path
    names = {v["name"] for v in json.loads((Path(__file__).parent / "config_contract.json").read_text())["variables"]}
    assert REFERENCE_ENV <= names, REFERENCE_ENV - names


# ── a live CrowdSec alert carries a countdown; reading it again must not open another case ───────────────────────

def test_the_same_live_alert_read_twice_is_one_case_even_though_its_decision_duration_counts_down():
    live = importlib.import_module("edge-sentinel.live")
    CaseStore = importlib.import_module("edge-sentinel.case_store").CaseStore

    def alert(duration):
        return {"scenario": "crowdsecurity/ssh-bf", "source": {"value": "203.0.113.9", "scope": "Ip"}, "created_at": "2026-11-13T17:46:17Z",
                "events_count": 1, "decisions": [{"type": "ban", "value": "203.0.113.9", "scope": "Ip", "duration": duration}]}
    st = CaseStore()
    first = live.sync_cases(st, alerts_fn=lambda: [alert("3h59m55s")])
    second = live.sync_cases(st, alerts_fn=lambda: [alert("3h58m02s")])
    assert first == second and len(st.cases) == 1 and len(st.evidence) == 1 and len(st.actions) == 1
    detail = live.case_detail(first[0], st)
    assert detail["replay"]["deterministic"] is True


def test_a_signed_in_standalone_dashboard_shows_no_fictional_tenant_and_no_demo_pill(gated, monkeypatch):
    html = gated.get("/", auth=CRED).text
    assert "CONTEXT RUNTIME" in html and ">DEMO<" not in html
    assert "Meridian" not in html and "demo tenant" not in html          # no fictional company by default
    assert "<title>Edge Sentinel</title>" in html
    monkeypatch.delenv("SENTINEL_REQUIRE_AUTH")
    assert ">DEMO<" in TestClient(app_mod.app).get("/").text            # the reference page keeps its banner
    monkeypatch.setattr(app_mod, "TENANT", "Acme Security")             # the operator-set name appears where the name belongs
    page = TestClient(app_mod.app).get("/").text
    assert "<title>Edge Sentinel — Acme Security</title>" in page and "<b>Acme Security</b>" in page
    monkeypatch.setenv("SENTINEL_TENANT", "  " + "x" * 100)
    import importlib as _il
    assert len(_il.reload(core).TENANT) == 80
    monkeypatch.delenv("SENTINEL_TENANT")
    assert _il.reload(core).TENANT == ""
