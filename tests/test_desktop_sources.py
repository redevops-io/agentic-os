"""Desktop source registration: the sidecar's own control authority, usable by the launch-capability session for folder registration only.

Context: behind the local capability gate (the desktop sidecar) ``POST /api/sources/confirm`` answered 503 ("AGENTIC_OS_CONTROL_TOKEN is not
set") because origin/main's control-token check sits behind a gate that offers no way to supply one. The design under test
(``agentic_os/desktop_sources.py``): a random per-process token in memory that no route, header, log, audit record or environment variable ever
carries; used server-side, after the gate has validated the session, for the enumerated folder-registration routes only; folders are exactly
what the user picked in a proposal, checked by ``source_folder_policy``; every registration, refusal and removal is a hash-chained, anchored
audit record; removal (and the end of the session) takes effect on the next request.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from agentic_os import desktop_sources as ds
from agentic_os.local_capability import SESSION_SCOPE_KEY, LocalCapability, Session, b64url
from agentic_os.projects_api import SampleProjectionProvider, create_app

PORT = 8787
HOST = f"127.0.0.1:{PORT}"
BASE = f"http://{HOST}"
PROJECT = "customer-ops"
ENUMERATED = {"POST /api/sources/confirm", "POST /api/sources/remove"}


# ----------------------------------------------------------------------------------------------------------------------------------
# a desktop sidecar in miniature: the real gate, the real routes, a synthetic home, a synthetic audit location
# ----------------------------------------------------------------------------------------------------------------------------------
class World:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.home = tmp_path / "home" / "alex"
        self.home.mkdir(parents=True)
        self.audit_path = tmp_path / "state" / "audit" / "source-registrations.jsonl"
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("USERPROFILE", str(self.home))
        monkeypatch.setenv("AGENTIC_OS_SOURCES_AUDIT_PATH", str(self.audit_path))
        for k in ("AGENTIC_OS_CONTROL_TOKEN", "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS", "PROJECTS_SOURCE_ROOTS", "AGENT_GATEWAY_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        self.mp = monkeypatch
        self.cap = LocalCapability(port=PORT)
        self.provider = SampleProjectionProvider()
        self.app = create_app(self.provider, capability=self.cap)
        self.client, self.csrf = self.session()

    def session(self, cap=None):
        cap = cap or self.cap
        c = TestClient(self.app, base_url=BASE, raise_server_exceptions=False)
        r = c.post("/api/local/session", json={"code": cap.issue_launch_code()})
        assert r.status_code == 200, r.text
        j = r.json()
        c.headers["X-Agentic-Session"] = j["session_token"]
        return c, j["csrf_token"]

    def folder(self, name="docs", files=("a.pdf",)) -> Path:
        d = self.tmp / name
        d.mkdir(exist_ok=True)
        for f in files:
            (d / f).write_text("x")
        return d

    def propose(self, folder, client=None, csrf=None, project=PROJECT):
        c = client or self.client
        return c.post("/api/sources/propose", json={"project_id": project, "text": f"use the files in {folder} as context"},
                      headers={"X-Agentic-CSRF": csrf or self.csrf})

    def confirm(self, location, client=None, csrf=None, project=PROJECT, **headers):
        c = client or self.client
        return c.post("/api/sources/confirm", json={"project_id": project, "confirmed_by": "you",
                                                    "sources": [{"kind": "files", "location": str(location)}]},
                      headers={"X-Agentic-CSRF": csrf or self.csrf, **headers})

    def pick(self, folder, **kw):
        assert self.propose(folder, **{k: v for k, v in kw.items() if k in ("client", "csrf", "project")}).status_code == 200
        return self.confirm(folder, **kw)

    def listing(self, client=None, project=PROJECT):
        return (client or self.client).get(f"/api/projects/{project}/sources").json()

    def mine(self, folder, client=None, project=PROJECT):
        return [s for s in self.listing(client, project) if s["location"] == os.path.realpath(folder)]

    def remove(self, source_id, client=None, csrf=None, project=PROJECT):
        return (client or self.client).post("/api/sources/remove", json={"project_id": project, "source_id": source_id},
                                            headers={"X-Agentic-CSRF": csrf or self.csrf})

    def audit(self):
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text().splitlines() if line.strip()]

    @property
    def authority(self):
        return self.app.state.desktop_sources


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def request_with(headers=None, scope_extra=None, method="POST", path="/api/sources/confirm") -> Request:
    given = {k.lower(): v for k, v in (headers or {}).items()}
    given.setdefault("host", HOST)
    hdrs = [(k.encode(), v.encode()) for k, v in given.items()]
    scope = {"type": "http", "method": method, "path": path, "headers": hdrs, "query_string": b"", "client": ("127.0.0.1", 1), "server": ("127.0.0.1", PORT)}
    scope.update(scope_extra or {})
    return Request(scope)


# ----------------------------------------------------------------------------------------------------------------------------------
# the flow the Sidekick drives: propose -> "Looks right - connect" -> listed this session only -> removed
# ----------------------------------------------------------------------------------------------------------------------------------
def test_a_proposed_folder_can_be_confirmed_by_the_launch_session_alone_and_is_listed_this_session_only(w):
    d = w.folder()
    r = w.pick(d)
    assert r.status_code == 200, r.text
    assert r.json()[0]["stats"]["indexed"] == 1
    mine = w.mine(d)
    assert len(mine) == 1 and mine[0]["name"].endswith("(this session only)") and mine[0]["session_only"] is True
    assert "AGENTIC_OS_CONTROL_TOKEN" not in os.environ                       # nothing was set in the environment to make this work


def test_a_confirm_without_a_proposal_is_refused_403_not_proposed_and_nothing_is_scanned(w, monkeypatch):
    d = w.folder()
    walked = []
    real = os.walk
    monkeypatch.setattr(os, "walk", lambda *a, **k: walked.append(a[0]) or real(*a, **k))
    r = w.confirm(d)
    assert r.status_code == 403
    assert r.json() == {"detail": {"code": "not_proposed", "message": "Only a folder you picked in a proposal can be connected. Ask again, then confirm.", "path": str(d)}}
    assert walked == [] and w.mine(d) == []


def test_other_spellings_of_the_proposed_folder_are_the_same_folder(w):
    d = w.folder()
    w.propose(d)
    for spelling in (f"{d}/", f"{d}//", f"{d.parent}/./docs", f"{d.parent}/x/../docs"):
        assert w.confirm(spelling).status_code == 200, spelling


def test_a_proposal_is_for_that_folder_that_session_and_that_project(w):
    a, b = w.folder("a"), w.folder("b")
    w.propose(a)
    assert w.confirm(b).json()["detail"]["code"] == "not_proposed"                  # a different folder
    assert w.confirm(a, project="other-project").json()["detail"]["code"] == "not_proposed"      # a different project
    c2, csrf2 = w.session()
    assert w.confirm(a, client=c2, csrf=csrf2).json()["detail"]["code"] == "not_proposed"        # a different session
    assert w.confirm(a).status_code == 200                                         # the proposing session


def test_a_proposal_expires(w):
    d = w.folder()
    w.propose(d)
    real = ds.time.time
    w.mp.setattr(ds.time, "time", lambda: real() + ds.PROPOSAL_TTL_S + 5)
    assert w.confirm(d).json()["detail"]["code"] == "not_proposed"


def test_two_folders_are_two_sources_and_removing_one_leaves_the_other(w):
    a, b = w.folder("a"), w.folder("b", files=("x.pdf", "y.pdf"))
    assert w.pick(a).status_code == 200 and w.pick(b).status_code == 200
    ma, mb = w.mine(a), w.mine(b)
    assert len(ma) == 1 and len(mb) == 1 and ma[0]["source_id"] != mb[0]["source_id"]
    assert w.remove(ma[0]["source_id"]).status_code == 200
    assert w.mine(a) == [] and len(w.mine(b)) == 1
    assert w.confirm(a).status_code == 200                                         # still proposed: a re-register is allowed within the proposal's life
    assert len(w.mine(a)) == 1


def test_registering_the_same_folder_twice_lists_it_once(w):
    d = w.folder()
    w.pick(d)
    assert w.confirm(d).status_code == 200
    assert len(w.mine(d)) == 1


def test_non_file_sources_pass_through_unchanged(w):
    r = w.client.post("/api/sources/confirm", json={"project_id": PROJECT, "confirmed_by": "you", "sources": [{"kind": "cloud_files", "location": "Google Drive", "provider": "google_drive"}]},
                      headers={"X-Agentic-CSRF": w.csrf})
    assert r.status_code == 200 and r.json()[0]["health"]["state"] in ("degraded", "pending", "error", "healthy")
    assert w.audit() == []                                                          # nothing on the user's disk was registered, nothing to audit


def test_a_bad_kind_is_still_a_422_in_the_existing_shape(w):
    r = w.client.post("/api/sources/confirm", json={"project_id": PROJECT, "confirmed_by": "you", "sources": [{"kind": "bogus", "location": "/x"}]}, headers={"X-Agentic-CSRF": w.csrf})
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


# ----------------------------------------------------------------------------------------------------------------------------------
# folder policy through HTTP: the refusal is a 4xx with a fixed body
# ----------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("spell,code,status", [
    ("{home}", "home_directory", 403), ("{home}/.ssh", "protected_location", 403), ("{home}/.aws/sub", "protected_location", 403), ("/", "filesystem_root", 403),
    ("{tmp}/missing", "not_found", 422), ("{file}", "not_a_directory", 422), ("relative/dir", "not_absolute", 422), ("{home}/..", "contains_protected_location", 403),
    ("//./pipe/x", "device_path", 403), ("\\\\server\\C$\\x", "unc_admin_share", 403), ("C:\\", "drive_root", 403), ("{tmp}/d*", "invalid_path", 422),
])
def test_the_policy_refuses_over_http_with_a_4xx_and_the_fixed_body(w, spell, code, status):
    (w.home / ".ssh").mkdir(exist_ok=True)
    (w.home / ".aws" / "sub").mkdir(parents=True, exist_ok=True)
    f = w.tmp / "afile"
    f.write_text("x")
    loc = spell.format(home=w.home, tmp=w.tmp, file=f)
    w.propose(loc)                                                                  # even a proposed folder is subject to the policy
    r = w.confirm(loc)
    assert r.status_code == status and status < 500, (loc, r.status_code, r.text)
    body = r.json()["detail"]
    assert set(body) == {"code", "message", "path"} and body["code"] == code
    assert not [s for s in w.listing() if s.get("session_only")]                     # nothing was registered


def test_a_symlink_to_a_protected_folder_is_refused_and_its_real_path_is_not_in_the_response(w):
    ssh = w.home / ".ssh"
    ssh.mkdir()
    link = w.tmp / "innocent"
    os.symlink(ssh, link)
    w.propose(link)
    r = w.confirm(link)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "protected_location" and r.json()["detail"]["path"] == str(link)
    assert str(ssh) not in r.text
    rec = [x for x in w.audit() if x["action"] == "refuse"][-1]
    assert rec["real_path"] == os.path.realpath(ssh) and rec["path"] == str(link)    # the audit has both


def test_the_apps_own_audit_directory_cannot_be_registered(w):
    w.audit_path.parent.mkdir(parents=True)
    w.propose(w.audit_path.parent)
    assert w.confirm(w.audit_path.parent).json()["detail"]["code"] == "protected_location"
    w.propose(w.audit_path.parent.parent)
    assert w.confirm(w.audit_path.parent.parent).json()["detail"]["code"] == "contains_protected_location"


# ----------------------------------------------------------------------------------------------------------------------------------
# PROJECTS_SOURCE_ROOTS: an explicit configuration still restricts and is not widened
# ----------------------------------------------------------------------------------------------------------------------------------
def test_an_explicit_source_root_restricts_the_session_and_is_not_widened(w):
    allowed, other = w.folder("allowed-root"), w.folder("other")
    inside = allowed / "docs"
    inside.mkdir()
    w.mp.setenv("PROJECTS_SOURCE_ROOTS", str(allowed))
    w.propose(other)
    r = w.confirm(other)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "outside_allowed_roots"      # proposed, picked, and still outside the operator's roots
    assert w.mine(other) == []
    assert w.confirm(inside).status_code == 200                                               # inside: as on main, no proposal needed
    (allowed / ".ssh").mkdir()
    assert w.confirm(allowed / ".ssh").json()["detail"]["code"] in ("protected_location", "outside_allowed_roots")


def test_roots_do_not_switch_the_policy_off_for_a_protected_folder_inside_them(w):
    w.mp.setenv("PROJECTS_SOURCE_ROOTS", str(w.tmp))
    (w.home / ".gnupg").mkdir()
    r = w.confirm(w.home / ".gnupg")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "protected_location"


# ----------------------------------------------------------------------------------------------------------------------------------
# the token: random per start, in memory, never exposed
# ----------------------------------------------------------------------------------------------------------------------------------
def token_needles(authority):
    raw = authority._token
    return {raw, b64url(raw).encode(), raw.hex().encode(), base64.b64encode(raw), base64.standard_b64encode(raw).rstrip(b"=")}


def test_the_token_is_at_least_256_bits_random_per_start_and_not_the_capability_secret(w, tmp_path, monkeypatch):
    a = w.authority
    assert isinstance(a._token, bytes) and len(a._token) >= 32
    other = create_app(SampleProjectionProvider(), capability=LocalCapability(port=PORT)).state.desktop_sources
    assert other._token != a._token and a._token != w.cap._secret
    assert len({create_app(SampleProjectionProvider(), capability=LocalCapability(port=PORT)).state.desktop_sources._token for _ in range(5)}) == 5


def test_an_explicit_control_token_means_no_automatic_one_and_behaviour_is_main_s(w):
    w.mp.setenv("AGENTIC_OS_CONTROL_TOKEN", "explicit-token-" + "x" * 30)
    assert w.authority.available() is False
    d = w.folder()
    w.propose(d)
    r = w.confirm(d)
    assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer"                  # main: outside every root, no Bearer
    ok = w.confirm(d, Authorization="Bearer explicit-token-" + "x" * 30)
    assert ok.status_code == 200
    assert w.audit() == []                                                                          # the desktop authority did not act


def test_tenant_and_approver_tokens_also_switch_the_automatic_token_off(w):
    for env in ("AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS"):
        w.mp.setenv(env, "acme=" + "t" * 32)
        assert w.authority.available() is False
        w.mp.delenv(env)
        assert w.authority.available() is True
    w.mp.setenv("AGENTIC_OS_TENANT_TOKENS", "malformed")
    w.mp.setenv("AGENTIC_OS_TENANT_LIMITS", "{not json")
    assert isinstance(w.authority.available(), bool)                                                 # a bad config never raises here


def test_the_hosted_app_has_no_authority_and_a_session_header_means_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)
    monkeypatch.delenv("PROJECTS_SOURCE_ROOTS", raising=False)
    app = create_app(SampleProjectionProvider())
    assert app.state.desktop_sources is None
    d = tmp_path / "docs"
    d.mkdir()
    c = TestClient(app, base_url=BASE, raise_server_exceptions=False)
    body = {"project_id": PROJECT, "confirmed_by": "x", "sources": [{"kind": "files", "location": str(d)}]}
    assert c.post("/api/sources/confirm", json=body, headers={"X-Agentic-Session": "anything", SESSION_SCOPE_KEY: "x"}).status_code == 503
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "hosted-token-" + "h" * 30)
    assert c.post("/api/sources/confirm", json=body).status_code == 401
    assert c.post("/api/sources/confirm", json=body, headers={"Authorization": "Bearer hosted-token-" + "h" * 30}).status_code == 200
    r = c.post("/api/sources/remove", json={"project_id": PROJECT, "source_id": "nope"})
    assert r.status_code == 401                                                                    # hosted removal needs the Bearer token, like a confirm outside the roots


def test_the_token_is_nowhere_a_client_a_log_the_audit_the_environment_or_a_child_can_see(w, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    d = w.folder()
    needles = token_needles(w.authority)
    seen = []

    def check(r, what):
        blob = (r.text + json.dumps({k: v for k, v in r.headers.items()})).encode()
        for n in needles:
            assert n not in blob, what
        seen.append(what)
    check(w.propose(d), "propose")
    check(w.confirm(d), "confirm")
    check(w.confirm(w.tmp / "missing"), "refused confirm")
    check(w.confirm(w.home), "refused home")
    check(w.remove("nope"), "remove 404")
    check(w.remove(w.mine(d)[0]["source_id"]), "remove")
    # every GET route the app has, with a session, and the error paths
    for route in w.app.routes:
        path = getattr(route, "path", "")
        if "GET" in getattr(route, "methods", ()) and "{" not in path and not path.startswith(("/docs", "/redoc", "/openapi")):
            check(w.client.get(path), path)
    check(w.client.get("/openapi.json"), "openapi")
    check(w.client.get("/api/local/session"), "session")
    check(TestClient(w.app, base_url=BASE).get("/api/projects"), "no session")
    assert len(seen) > 12
    # audit file, logs, captured output, environment, process arguments, a child's environment
    audit = w.audit_path.read_bytes()
    logs = (caplog.text + capfd.readouterr().out + capfd.readouterr().err).encode()
    env_blob = "\n".join(f"{k}={v}" for k, v in os.environ.items()).encode()
    child = subprocess.run([sys.executable, "-c", "import os,sys; sys.stdout.write(repr(dict(os.environ)))"], capture_output=True, env=None).stdout
    args = " ".join(sys.argv).encode()
    for n in needles:
        assert n not in audit and n not in logs and n not in env_blob and n not in child and n not in args
    assert "AGENTIC_OS_CONTROL_TOKEN" not in os.environ
    assert repr(w.authority).find(b64url(w.authority._token)) < 0 and str(w.authority).find(w.authority._token.hex()) < 0


def test_the_authority_cannot_be_pickled_copied_or_serialised(w):
    import copy
    import pickle
    for fn in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            fn(w.authority)


# ----------------------------------------------------------------------------------------------------------------------------------
# who may use it: the validated launch session, server side, for the enumerated routes only
# ----------------------------------------------------------------------------------------------------------------------------------
def test_without_a_session_a_forged_session_or_a_replayed_one_the_routes_refuse_and_nothing_happens(w):
    d = w.folder()
    w.propose(d)
    anon = TestClient(w.app, base_url=BASE, raise_server_exceptions=False)
    body = {"project_id": PROJECT, "confirmed_by": "x", "sources": [{"kind": "files", "location": str(d)}]}
    for route, payload in (("/api/sources/confirm", body), ("/api/sources/remove", {"project_id": PROJECT, "source_id": "x"})):
        assert anon.post(route, json=payload).status_code == 401
        assert anon.post(route, json=payload, headers={"X-Agentic-Session": "forged-" + "a" * 40, "X-Agentic-CSRF": w.csrf}).status_code == 401
        other_cap = LocalCapability(port=PORT)
        tok = other_cap.new_session(HOST)[0]                                    # a real session, but of another process
        assert anon.post(route, json=payload, headers={"X-Agentic-Session": tok, "X-Agentic-CSRF": w.csrf}).status_code == 401
    # a session that was signed out cannot be replayed
    c, csrf = w.session()
    tok = c.headers["X-Agentic-Session"]
    assert c.delete("/api/local/session", headers={"X-Agentic-CSRF": csrf}).status_code == 200
    assert w.confirm(d, client=c, csrf=csrf).status_code == 401
    replay = TestClient(w.app, base_url=BASE, raise_server_exceptions=False)
    assert replay.post("/api/sources/confirm", json=body, headers={"X-Agentic-Session": tok, "X-Agentic-CSRF": csrf}).status_code == 401
    assert w.mine(d) == [] and w.audit() == []


def test_the_csrf_header_is_required_and_a_session_from_another_host_is_not_accepted(w):
    d = w.folder()
    w.propose(d)
    assert w.client.post("/api/sources/confirm", json={"project_id": PROJECT, "confirmed_by": "x", "sources": [{"kind": "files", "location": str(d)}]}).status_code == 403
    r = w.confirm(d, Host="localhost:9999")
    assert r.status_code == 400                                                  # DNS-rebinding style Host
    for host in ("evil.example", "evil.example:8787", f"127.0.0.1.evil.example:{PORT}", "[::1]:1", ""):
        assert w.confirm(d, Host=host).status_code in (400, 401, 403), host
    assert w.mine(d) == []


def test_cross_origin_and_origin_mismatch_are_refused(w):
    d = w.folder()
    w.propose(d)
    for origin in ("https://evil.example", "null", f"http://127.0.0.1:{PORT + 1}", f"http://localhost:{PORT}", f"https://{HOST}", "http://127.0.0.1"):
        r = w.confirm(d, Origin=origin)
        assert r.status_code == 403, origin
        assert not [k for k in r.headers if k.lower().startswith("access-control-")]
    assert w.confirm(d, **{"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert w.confirm(d, **{"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert w.mine(d) == [] and w.audit() == []
    assert w.confirm(d, Origin=BASE, **{"Sec-Fetch-Site": "same-origin"}).status_code == 200      # the browser's own request


def test_the_route_rechecks_host_origin_and_fetch_site_itself_even_if_the_gate_were_bypassed(w):
    live = w.cap.new_session(HOST)[1]
    scope = {SESSION_SCOPE_KEY: live}

    def auth(headers=None, route="POST /api/sources/confirm", extra=None):
        return w.authority.authorization_for(request_with(headers, {**scope, **(extra or {})}), route)
    assert auth() is not None
    assert auth({"origin": BASE}) is not None
    assert auth({"origin": "https://evil.example"}) is None and auth({"origin": "null"}) is None
    assert auth({"origin": f"http://localhost:{PORT}"}) is None                                       # not the Host the request arrived on
    assert auth({"sec-fetch-site": "cross-site"}) is None
    assert auth({"origin": BASE, "sec-fetch-site": "same-origin"}) is not None
    assert w.authority.authorization_for(request_with({"host": "evil.example"}, scope), "POST /api/sources/confirm") is None
    r = request_with(None, scope)
    r.scope["headers"] = r.scope["headers"] + [(b"host", HOST.encode())]                              # two Host headers
    assert w.authority.authorization_for(r, "POST /api/sources/confirm") is None


def test_only_a_live_session_object_the_gate_itself_issued_unlocks_the_authority(w):
    live = w.cap.new_session(HOST)[1]
    ok = lambda sess, route="POST /api/sources/confirm": w.authority.authorization_for(request_with(None, {SESSION_SCOPE_KEY: sess} if sess is not None else {}), route)
    assert ok(live) is not None
    assert ok(None) is None
    assert ok("a string") is None and ok(1) is None and ok({"csrf": live.csrf}) is None
    lookalike = Session(csrf=live.csrf, expires=live.expires, host=live.host)
    assert ok(lookalike) is None                                                                       # same fields, not issued by the capability
    foreign = LocalCapability(port=PORT).new_session(HOST)[1]
    assert ok(foreign) is None                                                                         # issued by another capability
    w.cap.drop_session(live)
    assert ok(live) is None                                                                            # signed out
    expired = w.cap.new_session(HOST)[1]
    expired.expires = (0.0, 0.0)
    assert ok(expired) is None


def test_the_authority_answers_only_for_the_enumerated_routes(w):
    live = w.cap.new_session(HOST)[1]
    scope = {SESSION_SCOPE_KEY: live}
    for route in sorted(ENUMERATED):
        assert w.authority.authorization_for(request_with(None, scope), route) is not None, route
    for route in ("POST /api/outcomes", "POST /api/sidekick", "POST /api/apps/twenty/connect", "POST /api/apps/twenty/connect/start", "POST /api/projects/p/policy/promotion-cycle",
                  "POST /api/projects/p/today/batch", "POST /api/projects/p/today/i/approve", "PUT /api/projects/p/today/notification-prefs", "POST /api/sources/propose",
                  "GET /api/projects", "DELETE /api/local/session", "POST /api/agent-gateway/mcp", "POST /api/app-directory/ref-link", "", "POST /api/sources/confirm/",
                  "post /api/sources/confirm", "POST /api/sources/confirm?x=1", "POST  /api/sources/confirm"):
        assert w.authority.authorization_for(request_with(None, scope), route) is None, route
    # the method on the request must be the route's method
    assert w.authority.authorization_for(request_with(None, scope, method="GET"), "POST /api/sources/confirm") is None
    assert ds.ROUTES == frozenset(ENUMERATED)


def test_every_other_mutating_route_behaves_exactly_as_it_did_for_the_local_session(w, monkeypatch):
    """With the authority present, absent, or turned off, the status of every other write route is identical, and the authority was asked only for the enumerated ones."""
    granted = []
    real = ds.DesktopSourceAuthority.authorization_for

    def spy(self, request, route):
        out = real(self, request, route)
        granted.append((route, out is not None))
        return out
    monkeypatch.setattr(ds.DesktopSourceAuthority, "authorization_for", spy)
    routes = []
    for r in w.app.routes:
        for m in sorted(getattr(r, "methods", ()) or ()):
            p = getattr(r, "path", "")
            if m in ("POST", "PUT", "PATCH", "DELETE") and not p.startswith("/api/local/") and f"{m} {p}" not in ENUMERATED:
                routes.append((m, p))
    assert len(routes) > 15
    fill = {"provider": "twenty", "project_id": "p1", "item_id": "i1", "capture_id": "c1", "name": "x"}

    def run():
        out = {}
        for m, p in routes:
            url = p
            for k, v in fill.items():
                url = url.replace("{" + k + "}", v)
            r = w.client.request(m, url, json={}, headers={"X-Agentic-CSRF": w.csrf})
            out[(m, p)] = r.status_code
        return out
    with_authority = run()
    monkeypatch.setattr(ds.DesktopSourceAuthority, "available", lambda self: False)
    without = run()
    assert with_authority == without
    assert not [g for g in granted if g[1]], granted                                                  # never granted to a route outside the set


def test_nothing_the_client_sends_can_select_or_supply_the_authority(w):
    live_hdrs = {"Authorization": "Bearer " + "z" * 43, "X-Control-Token": "z" * 43, "Cookie": "control_token=" + "z" * 43, SESSION_SCOPE_KEY: "x", "X-Agentic-Session-Scope": "x"}
    # a non-enumerated route stays refused for the local session whatever it sends, in any header, query parameter or body field
    url = "/api/projects/customer-ops/policy/promotion-cycle?control_token=zzz&token=zzz&authorization=zzz&apply=true"
    for payload in ({"apply": True}, {"apply": True, "authorization": "Bearer zzz", "token": "zzz", "control_token": "zzz", "tenant_id": "t"}):
        r = w.client.post(url, json=payload, headers={"X-Agentic-CSRF": w.csrf, **live_hdrs})
        assert r.status_code in (401, 503), r.text
    r = w.client.post("/api/outcomes", json={"authoritative": True, "intervention_id": "i", "idempotency_key": "k", "source_app": "a", "action_kind": "k"},
                      headers={"X-Agentic-CSRF": w.csrf, **live_hdrs})
    assert r.status_code in (401, 503), r.text
    # and the scope key an attacker could try to inject over the wire is stripped by the gate, so an unauthenticated request stays unauthenticated
    anon = TestClient(w.app, base_url=BASE, raise_server_exceptions=False)
    assert anon.post("/api/sources/confirm", json={"project_id": "p", "confirmed_by": "x", "sources": []}, headers=live_hdrs).status_code == 401


def test_a_body_field_cannot_name_a_folder_that_was_not_proposed_through_another_key(w):
    d = w.folder()
    r = w.client.post("/api/sources/confirm", headers={"X-Agentic-CSRF": w.csrf},
                      json={"project_id": PROJECT, "confirmed_by": "x", "proposed": True, "picked": True, "trusted": True, "sources": [{"kind": "files", "location": str(d), "proposed": True, "user_picked": True}]})
    assert r.status_code == 403


# ----------------------------------------------------------------------------------------------------------------------------------
# audit: every registration, refusal and removal; hash-chained and anchored; nothing secret
# ----------------------------------------------------------------------------------------------------------------------------------
def test_registration_refusal_and_removal_are_each_one_audit_record_with_the_documented_fields(w):
    d = w.folder()
    w.pick(d)
    w.confirm(w.home)
    sid = w.mine(d)[0]["source_id"]
    w.remove(sid)
    recs = w.audit()
    assert [(r["action"], r["result"], r["reason"]) for r in recs] == [("register", "ok", "ok"), ("refuse", "refused", "home_directory"), ("remove", "ok", "user_request")]
    reg = recs[0]
    assert reg["principal"] == "local-owner" and reg["method"] == "local-capability" and reg["scope"] == PROJECT and isinstance(reg["ts"], float)
    assert reg["path"] == str(d) and reg["real_path"] == os.path.realpath(d) and reg["source_id"] == sid
    assert recs[1]["path"] == str(w.home) and recs[1]["real_path"] == os.path.realpath(w.home)
    assert recs[2]["source_id"] == sid and recs[2]["path"] == str(d)
    blob = json.dumps(recs)
    assert "token" not in blob.lower() and "csrf" not in blob.lower() and "session" not in blob.lower().replace("session_ended", "")


def test_a_refusal_for_a_missing_proposal_and_for_a_roots_violation_is_recorded_too(w):
    d = w.folder()
    w.confirm(d)
    assert [(r["action"], r["reason"]) for r in w.audit()] == [("refuse", "not_proposed")]


def test_the_audit_log_is_a_verifiable_hash_chain_with_an_anchor_and_is_private(w):
    d = w.folder()
    w.pick(d)
    w.confirm(w.home)
    log = w.authority.audit
    rep = log.verify()
    assert rep["ok"] is True and rep["records"] == 2 and rep["anchored"] == 2 and rep["status"] == "ok"
    assert (Path(str(w.audit_path) + ".anchor")).exists()
    if os.name != "nt":
        assert oct(w.audit_path.stat().st_mode & 0o777) == "0o600" and oct(w.audit_path.parent.stat().st_mode & 0o777) == "0o700"
    lines = w.audit_path.read_text().splitlines()
    # an edited record breaks the chain
    edited = json.loads(lines[0])
    edited["path"] = "/somewhere/else"
    w.audit_path.write_text("\n".join([json.dumps(edited), lines[1]]) + "\n")
    assert log.verify()["ok"] is False
    # a log cut short of its anchor is reported
    w.audit_path.write_text(lines[0] + "\n")
    rep = log.verify()
    assert rep["ok"] is False and rep["status"] == "truncated"
    w.audit_path.unlink()
    assert log.verify()["status"] == "truncated"


def test_a_registration_that_cannot_be_recorded_is_refused_with_nothing_registered_but_a_removal_still_works(w):
    d = w.folder()
    w.pick(d)
    sid = w.mine(d)[0]["source_id"]
    # make the audit location unwritable: replace its directory with a file
    import shutil
    shutil.rmtree(w.audit_path.parent)
    w.audit_path.parent.write_text("not a directory")
    e = w.folder("e")
    w.propose(e)
    r = w.confirm(e)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "audit_unavailable" and w.mine(e) == []
    assert w.remove(sid).status_code == 200 and w.mine(d) == []                                       # revocation never waits for the audit


def test_the_default_audit_location_is_the_per_user_app_data_directory(tmp_path):
    h = tmp_path / "h"
    assert ds.default_audit_path({}, str(h), "darwin") == str(h / "Library" / "Application Support" / "io.redevops.agentic-apps" / "audit" / "source-registrations.jsonl")
    assert ds.default_audit_path({"LOCALAPPDATA": "D:\\L"}, "C:\\Users\\a", "win32").replace("/", "\\") == "D:\\L\\io.redevops.agentic-apps\\audit\\source-registrations.jsonl"
    assert ds.default_audit_path({"XDG_STATE_HOME": "/xdg"}, "/home/a", "linux") == "/xdg/io.redevops.agentic-apps/audit/source-registrations.jsonl"
    assert ds.default_audit_path({}, "/home/a", "linux") == "/home/a/.local/state/io.redevops.agentic-apps/audit/source-registrations.jsonl"
    assert ds.default_audit_path({ds.ENV_AUDIT_PATH: "/explicit/log.jsonl"}, "/home/a", "linux") == "/explicit/log.jsonl"
    # relative explicit paths are not used
    assert ds.default_audit_path({ds.ENV_AUDIT_PATH: "rel/log"}, "/home/a", "linux").startswith("/home/a/")


def test_nothing_but_the_audit_log_is_written_to_disk(w, monkeypatch):
    d = w.folder()
    work = w.tmp / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    before = {p for p in w.tmp.rglob("*") if p.is_file()}
    w.pick(d)
    after = {p for p in w.tmp.rglob("*") if p.is_file()}
    assert {p.name for p in after - before} <= {"source-registrations.jsonl", "source-registrations.jsonl.anchor"}
    assert list(work.iterdir()) == []


# ----------------------------------------------------------------------------------------------------------------------------------
# revocation: removal and the end of the session take effect on the next request
# ----------------------------------------------------------------------------------------------------------------------------------
def test_removal_takes_effect_on_the_next_request_and_a_second_removal_is_a_404(w):
    d = w.folder()
    w.pick(d)
    sid = w.mine(d)[0]["source_id"]
    r = w.remove(sid)
    assert r.status_code == 200 and r.json() == {"removed": True, "source_id": sid}
    assert w.mine(d) == []
    r = w.remove(sid)
    assert r.status_code == 404 and r.json()["detail"] == {"code": "source_not_found", "message": "No such registered source.", "path": ""}
    sample = [s for s in w.listing() if not s.get("session_only")]
    assert w.remove(sample[0]["source_id"]).status_code == 404                                         # the sample sources are not registrations


def test_removal_needs_the_session_the_csrf_header_and_the_same_project(w):
    d = w.folder()
    w.pick(d)
    sid = w.mine(d)[0]["source_id"]
    anon = TestClient(w.app, base_url=BASE, raise_server_exceptions=False)
    assert anon.post("/api/sources/remove", json={"project_id": PROJECT, "source_id": sid}).status_code == 401
    assert w.client.post("/api/sources/remove", json={"project_id": PROJECT, "source_id": sid}).status_code == 403          # no CSRF
    assert w.remove(sid, project="other-project").status_code == 404
    assert len(w.mine(d)) == 1


def test_signing_out_forgets_what_that_session_registered_on_the_next_request(w):
    d = w.folder()
    w.pick(d)
    c2, csrf2 = w.session()                                                                              # the user opened a second window
    assert len(w.mine(d, client=c2)) == 1
    assert w.client.delete("/api/local/session", headers={"X-Agentic-CSRF": w.csrf}).status_code == 200  # sign-out of the first
    assert w.mine(d, client=c2) == []
    assert [(r["action"], r["reason"]) for r in w.audit()][-1] == ("remove", "session_ended")
    # a new session in the same process starts with none of them, and the folder needs a new pick
    c3, csrf3 = w.session()
    assert w.mine(d, client=c3) == []
    assert w.confirm(d, client=c3, csrf=csrf3).json()["detail"]["code"] == "not_proposed"


def test_an_expired_session_forgets_its_registrations_too(w):
    d = w.folder()
    w.pick(d)
    c2, _ = w.session()
    live = w.authority._registrations[0].session
    live.expires = (0.0, 0.0)
    assert w.mine(d, client=c2) == []


def test_sources_are_in_memory_only_a_new_process_has_none(w):
    d = w.folder()
    w.pick(d)
    fresh = create_app(SampleProjectionProvider(), capability=LocalCapability(port=PORT))
    cap = fresh.state.desktop_sources.capability
    c = TestClient(fresh, base_url=BASE)
    c.headers["X-Agentic-Session"] = c.post("/api/local/session", json={"code": cap.issue_launch_code()}).json()["session_token"]
    assert [s for s in c.get(f"/api/projects/{PROJECT}/sources").json() if s.get("session_only")] == []
