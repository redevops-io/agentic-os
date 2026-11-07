"""The drill-down mounted in the Projects app: configuration, the session-to-principal bridge, the permission that is still
required, the runtime-controlled data-source report (GET /api/ui-config), and the hosted Bearer path.

Everything goes through the product mount (``projects_api.create_app``): the REAL local capability gate, the REAL permission plane
(encrypted grant store in a temp dir), the REAL audit sink and a REAL ``MissionRuntime`` mission. Nothing injects a credential."""
from __future__ import annotations

import json
import os

import jsonschema
import pytest
from fastapi.testclient import TestClient

from agentic_os.app_kit.route_authz import unclassified, write_routes
from agentic_os.drilldown import DrilldownService
from agentic_os.drilldown.mount import (LOCAL_METHOD, LOCAL_OWNER_ID, ControlTokenAuthenticator, DrilldownConfig, DrilldownConfigError,
                                        LocalSessionBridge, drilldown_from_env)
from agentic_os.drilldown.operator import grant_local_inspect, local_inspect_grants, revoke_local_inspect
from agentic_os.drilldown.schema_export import ui_config_schema
from agentic_os.evidence_query import JsonlAuditSink
from agentic_os.local_capability import LocalCapability
from agentic_os.mission.store import EventStore
from agentic_os.permissions import GrantStore
from agentic_os.projects_api import SampleProjectionProvider, create_app
from agentic_os.projects_route_policy import PROJECTS_ROUTE_POLICIES as POLICIES
from tests.drilldown_support import SCOPE, completed_mission, set_env

PORT = 8787
BASE_URL = f"http://127.0.0.1:{PORT}"
NOT_FOUND = {"detail": "Not Found"}


class W:
    pass


def world(tmp_path, monkeypatch, *, drilldown=True, grant=False, log_anchor=True):
    """A Projects app behind the real capability gate, with (or without) the drill-down configured, and a signed-in client."""
    set_env(monkeypatch)
    monkeypatch.delenv("AGENT_GATEWAY_TOKEN", raising=False)
    w = W()
    w.tmp = tmp_path
    w.log = str(tmp_path / "events.jsonl")
    _, w.mid = completed_mission(w.log)
    w.store = EventStore(path=w.log, strict=False, anchor=log_anchor)
    w.grants = GrantStore(str(tmp_path / "perm"))
    w.audit = JsonlAuditSink(str(tmp_path / "audit.jsonl"))
    w.cfg = DrilldownConfig(store=w.store, grants=w.grants, audit=w.audit, project_id=SCOPE) if drilldown else None
    w.cap = LocalCapability(port=PORT)
    w.app = create_app(SampleProjectionProvider(), capability=w.cap, drilldown=w.cfg)
    w.client = TestClient(w.app, base_url=BASE_URL)
    w.login = lambda c=w.client: _login(c, w.cap)
    w.login()
    if grant:
        grant_local_inspect(w.grants, SCOPE, operator="test-operator", audit=w.audit)
    w.base = f"/api/projects/{SCOPE}/missions/{w.mid}"
    return w


def _login(c, cap):
    r = c.post("/api/local/session", json={"code": cap.issue_launch_code()})
    assert r.status_code == 200, r.text
    c.headers["X-Agentic-Session"] = r.json()["session_token"]
    return r.json()["csrf_token"]


def flat(app):
    """[(top-level position, path, methods, tags)] for every route the app serves, including lazily included routers (FastAPI >= 0.16x)."""
    out = []

    def walk(routes, top):
        for r in routes:
            if hasattr(r, "effective_candidates"):
                walk(r.effective_candidates(), top)
                continue
            route = getattr(r, "original_route", r)
            path = getattr(r, "path", "") or getattr(getattr(r, "starlette_route", None), "path", "") or getattr(route, "path", "")
            out.append((top, path, set(getattr(route, "methods", None) or ()), list(getattr(route, "tags", None) or [])))
    for i, r in enumerate(app.router.routes):
        walk([r], i)
    return out


def dd_routes(app):
    return [x for x in flat(app) if "drilldown" in x[3]]


def sub(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def receipts(w):
    with open(w.audit.path, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


# ───────────────────────── mounting: absent unless configured ─────────────────────────
def test_unconfigured_app_has_no_drilldown_route_and_reports_sample(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, drilldown=False)
    assert dd_routes(w.app) == []
    r = w.client.get(w.base + "/timeline")
    assert r.status_code == 404
    cfg = w.client.get("/api/ui-config").json()
    assert cfg["data_source"] == "sample" and cfg["drilldown"]["mounted"] is False
    assert cfg["stores"] == [] and cfg["authority"]["configured"] is False


def test_configured_app_mounts_the_three_routes_and_reports_runtime(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    paths = sorted(x[1] for x in dd_routes(w.app))
    assert paths == sorted(f"/api/projects/{{project_id}}/missions/{{mission_id}}{p}" for p in ("/timeline", "/evidence", "/evidence/{evidence_id}"))
    cfg = w.client.get("/api/ui-config").json()
    assert cfg["data_source"] == "runtime" and cfg["drilldown"]["mounted"] is True


def test_the_drilldown_routes_are_registered_ahead_of_the_ui_mount(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    mount_ix = next(i for i, r in enumerate(w.app.router.routes) if type(r).__name__ == "Mount" and getattr(r, "path", None) in ("", "/"))
    assert max(x[0] for x in dd_routes(w.app)) < mount_ix
    assert next(x[0] for x in flat(w.app) if x[1] == "/api/ui-config") < mount_ix


def test_the_mount_adds_no_write_route_so_the_route_policy_registry_is_unchanged(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    assert unclassified(w.app, POLICIES) == []
    assert sorted(set(POLICIES) - set(write_routes(w.app))) == []
    mine = [x for x in flat(w.app) if "drilldown" in x[3] or x[1] == "/api/ui-config"]
    assert len(mine) == 4
    for _top, path, methods, _tags in mine:
        assert methods <= {"GET", "HEAD"}, path


def test_mode_is_runtime_only_when_the_store_is_configured_and_the_routes_are_mounted(tmp_path, monkeypatch):
    for with_cfg in (False, True):
        w = world(sub(tmp_path, str(with_cfg)), monkeypatch, drilldown=with_cfg)
        mounted = bool(dd_routes(w.app))
        assert (w.client.get("/api/ui-config").json()["data_source"] == "runtime") == (with_cfg and mounted)
    # a config object without a store mounts nothing and cannot claim runtime
    with pytest.raises(DrilldownConfigError):
        DrilldownConfig(store=None, grants=GrantStore(str(tmp_path / "g2")), audit=JsonlAuditSink(str(tmp_path / "a2")), project_id=SCOPE)


# ───────────────────────── GET /api/ui-config: a closed schema, only what the server verified ─────────────────────────
def test_ui_config_validates_against_its_closed_schema_in_both_modes(tmp_path, monkeypatch):
    for with_cfg in (False, True):
        w = world(sub(tmp_path, str(with_cfg)), monkeypatch, drilldown=with_cfg)
        body = w.client.get("/api/ui-config").json()
        jsonschema.validate(body, ui_config_schema())
        assert body["schema"] == "aos.ui-config/v1"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**body, "build_flag": "x"}, ui_config_schema())
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**body, "data_source": "recorded"}, ui_config_schema())


def test_ui_config_reports_the_integrity_the_server_actually_checked(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    body = w.client.get("/api/ui-config").json()
    s = body["stores"][0]
    assert s["role"] == "event_log" and s["kind"] == "EventStore" and s["durable"] is True
    assert s["integrity"]["status"] == "ok" and s["integrity"]["records"] == s["integrity"]["anchored_records"] > 0
    assert body["drilldown"]["integrity"] == s["integrity"]


def test_ui_config_integrity_follows_the_log_it_does_not_claim_more(tmp_path, monkeypatch):
    # unanchored log: says unanchored
    w = world(sub(tmp_path, "u"), monkeypatch, log_anchor=False)
    assert w.client.get("/api/ui-config").json()["stores"][0]["integrity"]["status"] == "unanchored"
    # truncated at the end: says truncated, and still never "ok"
    w = world(sub(tmp_path, "t"), monkeypatch)
    lines = open(w.log, "rb").read().splitlines(keepends=True)
    open(w.log, "wb").write(b"".join(lines[:-1]))
    st = w.client.get("/api/ui-config").json()["stores"][0]["integrity"]
    assert st["status"] == "truncated"
    # an in-memory store is not durable
    from agentic_os.mission.store import MemoryEventStore
    w2 = world(sub(tmp_path, "m"), monkeypatch)
    cfg = DrilldownConfig(store=MemoryEventStore(), grants=w2.grants, audit=w2.audit, project_id=SCOPE)
    app = create_app(SampleProjectionProvider(), capability=w2.cap, drilldown=cfg)
    c = TestClient(app, base_url=BASE_URL)
    _login(c, w2.cap)
    s = c.get("/api/ui-config").json()["stores"][0]
    assert s["durable"] is False and s["integrity"]["status"] == "unanchored"


def test_ui_config_authority_store_status_comes_from_its_own_verify(tmp_path, monkeypatch):
    from agentic_os.mission.authority import AuthorityEnvelope, AutonomyBudget, Grant as AGrant, SqliteEnvelopeStore
    w = world(tmp_path, monkeypatch)
    db = str(tmp_path / "authority.db")
    auth = SqliteEnvelopeStore(db)
    auth.put(AuthorityEnvelope(tenant="acme", principal="owner", mission_id="m-1", issued_at=1.0, expires_at=2e9, grants=(AGrant("billing.*"),),
                               budget=AutonomyBudget(usd=1, max_actions=1)))
    cfg = DrilldownConfig(store=w.store, grants=w.grants, audit=w.audit, project_id=SCOPE, authority_store=auth)
    app = create_app(SampleProjectionProvider(), capability=w.cap, drilldown=cfg)
    c = TestClient(app, base_url=BASE_URL)
    _login(c, w.cap)
    a = c.get("/api/ui-config").json()["authority"]
    assert a == {"configured": True, "verified": True}
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("UPDATE authority_log SET body = body || ' '")
    con.commit()
    con.close()
    a = c.get("/api/ui-config").json()["authority"]
    assert a == {"configured": True, "verified": False}                     # a failed verification names nothing else


def test_ui_config_needs_a_session_under_the_gate(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    anon = TestClient(w.app, base_url=BASE_URL)
    assert anon.get("/api/ui-config").status_code == 401


# ───────────────────────── the bridge: capability session -> principal, then the permission is still required ─────────────────────────
def test_default_is_no_grant_a_valid_session_alone_reads_nothing(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    assert local_inspect_grants(w.grants, SCOPE) == []
    unknown = w.client.get(f"/api/projects/{SCOPE}/missions/m-nope/timeline")
    for suffix in ("/timeline", "/evidence", "/evidence/anything"):
        r = w.client.get(w.base + suffix)
        assert r.status_code == 404 and r.json() == NOT_FOUND
        assert r.content == unknown.content


def test_after_the_operator_grant_the_same_session_reads_all_three_surfaces(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, grant=True)
    t = w.client.get(w.base + "/timeline?limit=500")
    assert t.status_code == 200 and t.json()["provenance"]["origin"] == "event_log" and t.json()["provenance"]["sample_data"] is False
    lst = w.client.get(w.base + "/evidence")
    assert lst.status_code == 200 and lst.json()["items"]
    one = w.client.get(f"{w.base}/evidence/{lst.json()['items'][0]['id']}")
    assert one.status_code == 200


def test_revocation_takes_effect_on_the_next_request_of_the_same_session(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, grant=True)
    assert w.client.get(w.base + "/timeline").status_code == 200
    assert revoke_local_inspect(w.grants, SCOPE, operator="test-operator", audit=w.audit) == 1
    r = w.client.get(w.base + "/timeline")
    assert r.status_code == 404 and r.json() == NOT_FOUND
    # a cached validator is no way around a revoked grant
    grant_local_inspect(w.grants, SCOPE, operator="test-operator", audit=w.audit)
    etag = w.client.get(w.base + "/timeline").headers["etag"]
    revoke_local_inspect(w.grants, SCOPE, operator="test-operator", audit=w.audit)
    assert w.client.get(w.base + "/timeline", headers={"If-None-Match": etag}).status_code == 404


def test_a_grant_for_another_project_does_not_open_this_one(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    grant_local_inspect(w.grants, "other-project", operator="test-operator", audit=w.audit)
    assert w.client.get(w.base + "/timeline").status_code == 404
    assert w.client.get(f"/api/projects/other-project/missions/{w.mid}/timeline").status_code == 404     # the mission is not that project's


def test_no_session_no_principal_and_the_gate_answers_first(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, grant=True)
    anon = TestClient(w.app, base_url=BASE_URL)
    for hdrs in ({}, {"X-Agentic-Session": "not-a-real-session-token-0000000000000000"}):
        assert anon.get(w.base + "/timeline", headers=hdrs).status_code == 401


@pytest.mark.parametrize("header", ["X-Agentic-Principal", "X-Principal", "X-Forwarded-User", "X-Remote-User", "X-Local-Owner", "X-Authenticated-User"])
def test_a_request_header_cannot_invoke_or_choose_the_principal(tmp_path, monkeypatch, header):
    w = world(tmp_path, monkeypatch)                                       # signed in, NO grant
    r = w.client.get(w.base + "/timeline", headers={header: LOCAL_OWNER_ID})
    assert r.status_code == 404
    grant_local_inspect(w.grants, SCOPE, operator="op", audit=w.audit)
    ok = w.client.get(w.base + "/timeline", headers={header: "someone-else"})
    assert ok.status_code == 200                                           # the header changed nothing in either direction
    assert {x["principal"] for x in receipts(w) if x["kind"] == "drilldown.timeline"} == {LOCAL_OWNER_ID}


@pytest.mark.parametrize("query", ["principal=local-owner", "user=admin", "role=admin", "tenant=*", "session=1", "as=local-owner", "access_token=x"])
def test_a_query_parameter_cannot_invoke_or_choose_the_principal(tmp_path, monkeypatch, query):
    w = world(tmp_path, monkeypatch)
    r = w.client.get(w.base + f"/timeline?{query}")
    assert r.status_code in (404, 400) and r.status_code != 200
    grant_local_inspect(w.grants, SCOPE, operator="op", audit=w.audit)
    ok = w.client.get(w.base + f"/timeline?limit=3&{query}")
    assert ok.status_code == 200 and {x["principal"] for x in receipts(w) if x["kind"] == "drilldown.timeline"} == {LOCAL_OWNER_ID}


def test_under_the_gate_a_bearer_credential_is_not_a_second_way_in(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "admin-token-0123456789-abcdefghij")
    w = world(tmp_path, monkeypatch)
    hdr = {"Authorization": "Bearer admin-token-0123456789-abcdefghij", "X-Control-Token": "admin-token-0123456789-abcdefghij"}
    assert w.client.get(w.base + "/timeline", headers=hdr).status_code == 404            # no grant for local-owner: the token confers nothing here
    anon = TestClient(w.app, base_url=BASE_URL)
    assert anon.get(w.base + "/timeline", headers=hdr).status_code == 401              # and without a session the gate refuses first


def test_the_page_cannot_mint_a_principal_no_route_accepts_one(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    csrf = w.client.get("/api/local/session").json()["csrf_token"]
    for path in ("/api/ui-config", w.base + "/timeline", w.base + "/evidence"):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            r = w.client.request(method, path, headers={"X-Agentic-CSRF": csrf}, json={"principal": {"id": LOCAL_OWNER_ID}})
            assert r.status_code in (404, 405), (method, path, r.status_code)
    assert not [x for x in flat(w.app) if "principal" in x[1] or "grant" in x[1]]


# ───────────────────────── the bridge only believes the gate ─────────────────────────
def _request(scope_extra=None):
    from starlette.requests import Request
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"", **(scope_extra or {})}
    return Request(scope)


def test_bridge_ignores_a_request_the_gate_did_not_validate():
    from agentic_os.local_capability import SESSION_SCOPE_KEY
    cap = LocalCapability(port=PORT)
    b = LocalSessionBridge(cap)
    assert b.principal_of(_request()) is None
    assert b.principal_of(_request({SESSION_SCOPE_KEY: object()})) is None             # not a session
    assert b.principal_of(_request({SESSION_SCOPE_KEY: True})) is None


def test_bridge_ignores_a_session_the_capability_does_not_know_or_that_is_over(tmp_path, monkeypatch):
    from agentic_os.local_capability import SESSION_SCOPE_KEY, Session
    cap = LocalCapability(port=PORT)
    _tok, sess = cap.new_session("127.0.0.1:8787")
    b = LocalSessionBridge(cap)
    p = b.principal_of(_request({SESSION_SCOPE_KEY: sess}))
    assert (p.id, p.method, p.tenant, p.roles, p.kind) == (LOCAL_OWNER_ID, LOCAL_METHOD, "default", (), "user")
    from agentic_os.mission.principal import default_issuer
    assert default_issuer().verify(p)
    foreign = Session(csrf="x", expires=sess.expires, host=sess.host)                  # same shape, never issued by this capability
    assert b.principal_of(_request({SESSION_SCOPE_KEY: foreign})) is None
    other = LocalCapability(port=PORT)
    assert LocalSessionBridge(other).principal_of(_request({SESSION_SCOPE_KEY: sess})) is None   # issued by a different capability
    cap.drop_session(sess)
    assert b.principal_of(_request({SESSION_SCOPE_KEY: sess})) is None                 # signed out: no longer a principal


def test_bridge_does_not_outlive_the_session_expiry():
    from agentic_os.local_capability import SESSION_SCOPE_KEY
    t = {"now": 1000.0}
    cap = LocalCapability(port=PORT, clock=lambda: t["now"], wall_clock=lambda: t["now"], session_ttl=60)
    _tok, sess = cap.new_session("127.0.0.1:8787")
    b = LocalSessionBridge(cap)
    assert b.principal_of(_request({SESSION_SCOPE_KEY: sess})) is not None
    t["now"] += 61
    assert b.principal_of(_request({SESSION_SCOPE_KEY: sess})) is None


def test_expired_session_is_refused_by_the_gate_before_any_drilldown_code(tmp_path, monkeypatch):
    set_env(monkeypatch)
    t = {"now": 1000.0}
    w = world(tmp_path, monkeypatch, grant=True)
    cap = LocalCapability(port=PORT, clock=lambda: t["now"], wall_clock=lambda: t["now"], session_ttl=60)
    app = create_app(SampleProjectionProvider(), capability=cap, drilldown=w.cfg)
    c = TestClient(app, base_url=BASE_URL)
    _login(c, cap)
    assert c.get(w.base + "/timeline").status_code == 200
    t["now"] += 61
    assert c.get(w.base + "/timeline").status_code == 401


def test_an_inbound_scope_key_never_reaches_the_app_a_page_cannot_forge_a_validated_session(tmp_path, monkeypatch):
    """Even a server (or an outer layer) that put the key into the scope is overridden: the gate strips it and sets it only itself."""
    import asyncio
    from agentic_os.local_capability import SESSION_SCOPE_KEY
    w = world(tmp_path, monkeypatch, grant=True)
    _tok, sess = w.cap.new_session("127.0.0.1:8787")
    status = {}

    async def run():
        scope = {"type": "http", "method": "GET", "path": w.base + "/timeline", "raw_path": (w.base + "/timeline").encode(), "query_string": b"",
                 "headers": [(b"host", b"127.0.0.1:8787")], "scheme": "http", "http_version": "1.1", "server": ("127.0.0.1", PORT),
                 "client": ("127.0.0.1", 1), "root_path": "", SESSION_SCOPE_KEY: sess}      # a validated-looking session, but no X-Agentic-Session header

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            if m["type"] == "http.response.start":
                status["code"] = m["status"]
        await w.app(scope, receive, send)
    asyncio.run(run())
    assert status["code"] == 401


# ───────────────────────── hosted deployments: Bearer through control_auth, unchanged ─────────────────────────
def hosted(tmp_path, monkeypatch, **cfg_kw):
    set_env(monkeypatch)
    w = W()
    w.log = str(tmp_path / "events.jsonl")
    _, w.mid = completed_mission(w.log)
    w.store = EventStore(path=w.log, strict=False)
    w.grants = GrantStore(str(tmp_path / "perm"))
    w.audit = JsonlAuditSink(str(tmp_path / "audit.jsonl"))
    w.cfg = DrilldownConfig(store=w.store, grants=w.grants, audit=w.audit, project_id=SCOPE, **cfg_kw)
    w.app = create_app(SampleProjectionProvider(), drilldown=w.cfg)
    w.client = TestClient(w.app)
    w.base = f"/api/projects/{SCOPE}/missions/{w.mid}"
    return w


def test_hosted_requires_a_bearer_credential_and_a_session_header_is_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "admin-token-0123456789-abcdefghij")
    w = hosted(tmp_path, monkeypatch)
    for hdr in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic abc"}, {"X-Agentic-Session": "x" * 40}, {"X-Agentic-Principal": "admin"}):
        r = w.client.get(w.base + "/timeline", headers=hdr)
        assert r.status_code == 401 and r.json() == {"error": "authentication required"}, hdr


def test_hosted_control_token_still_needs_the_inspect_grant_no_admin_bypass(tmp_path, monkeypatch):
    tok = "admin-token-0123456789-abcdefghij"
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", tok)
    w = hosted(tmp_path, monkeypatch)
    h = {"Authorization": f"Bearer {tok}"}
    assert w.client.get(w.base + "/timeline", headers=h).status_code == 404
    w.grants.add("user", "admin", "tool", f"evidence.{SCOPE}.inspect", actions=["read"], created_by="test")
    r = w.client.get(w.base + "/timeline", headers=h)
    assert r.status_code == 200
    assert {x["principal"] for x in receipts(w) if x["kind"] == "drilldown.timeline" and x["outcome"] == "ok"} == {"admin"}


def test_hosted_tenant_tokens_are_tenant_principals_and_a_mission_of_another_tenant_is_invisible(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", "acme=tok-acme-0123456789abcdef,beta=tok-beta-0123456789abcdef")
    w = hosted(tmp_path, monkeypatch)
    w.store.append("MissionCreated", "m-acme", {"goal": "g", "tenant": "acme", "policy_refs": []})
    for user in ("requester:acme", "requester:beta"):
        w.grants.add("user", user, "tool", f"evidence.{SCOPE}.inspect", actions=["read"], created_by="test")
    acme = {"Authorization": "Bearer tok-acme-0123456789abcdef"}
    beta = {"Authorization": "Bearer tok-beta-0123456789abcdef"}
    url = f"/api/projects/{SCOPE}/missions/m-acme/timeline"
    assert w.client.get(url, headers=acme).status_code == 200
    r = w.client.get(url, headers=beta)                                     # holds the grant, wrong tenant: the same 404 as an unknown mission
    assert r.status_code == 404 and r.content == w.client.get(f"/api/projects/{SCOPE}/missions/m-none/timeline", headers=beta).content


def test_control_token_authenticator_is_fail_closed(monkeypatch):
    for k in ("AGENTIC_OS_CONTROL_TOKEN", "AGENTIC_OS_TENANT_TOKENS", "AGENTIC_OS_APPROVER_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    a = ControlTokenAuthenticator()
    assert a.authenticate("anything") is None and a.authenticate(None) is None and a.authenticate("") is None   # nothing configured: nobody
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", "acme=same-token-0123456789,beta=same-token-0123456789")     # invalid config: nobody
    assert a.authenticate("same-token-0123456789") is None


def test_an_injected_authenticator_replaces_the_default_for_hosted_use(tmp_path, monkeypatch):
    from agentic_os.evidence_query import SingleOwnerTokenAuthenticator
    tok = "owner-token-0123456789-abcdefghij"
    w = hosted(tmp_path, monkeypatch, authenticator=SingleOwnerTokenAuthenticator(tok, "owner"))
    w.grants.add("user", "owner", "tool", f"evidence.{SCOPE}.inspect", actions=["read"], created_by="test")
    assert w.client.get(w.base + "/timeline", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert w.client.get(w.base + "/timeline").status_code == 401


def test_hosted_ui_config_gives_store_facts_only_to_an_authenticated_caller(tmp_path, monkeypatch):
    tok = "admin-token-0123456789-abcdefghij"
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", tok)
    w = hosted(tmp_path, monkeypatch)
    anon = w.client.get("/api/ui-config").json()
    assert anon["data_source"] == "runtime" and anon["stores"] == [] and anon["drilldown"]["integrity"] == {"status": "not_reported"}
    authed = w.client.get("/api/ui-config", headers={"Authorization": f"Bearer {tok}"}).json()
    assert authed["stores"] and authed["drilldown"]["integrity"]["status"] in ("ok", "unanchored", "lagging")
    jsonschema.validate(anon, ui_config_schema())
    jsonschema.validate(authed, ui_config_schema())


# ───────────────────────── configuration from the environment: explicit keys, no invented paths ─────────────────────────
def test_env_with_no_drilldown_key_is_not_configured(monkeypatch):
    for k in [k for k in os.environ if k.startswith("PROJECTS_DRILLDOWN_")]:
        monkeypatch.delenv(k)
    assert drilldown_from_env({}) is None


def test_env_with_all_keys_builds_a_config_on_the_named_paths(tmp_path, monkeypatch):
    set_env(monkeypatch)
    log = str(tmp_path / "ev.jsonl")
    completed_mission(log)
    env = {"PROJECTS_DRILLDOWN_EVENT_PATH": log, "PROJECTS_DRILLDOWN_PROJECT": SCOPE, "PROJECTS_DRILLDOWN_GRANTS_DIR": str(tmp_path / "perm"),
           "PROJECTS_DRILLDOWN_AUDIT_PATH": str(tmp_path / "audit.jsonl")}
    cfg = drilldown_from_env(env)
    assert isinstance(cfg, DrilldownConfig) and cfg.project_id == SCOPE and type(cfg.store).__name__ == "EventStore"
    assert cfg.store._path == log and cfg.audit.path == str(tmp_path / "audit.jsonl") and cfg.grants.dir == str(tmp_path / "perm")
    assert os.listdir(str(tmp_path / "perm")) is not None and local_inspect_grants(cfg.grants, SCOPE) == []        # default: no grant


@pytest.mark.parametrize("missing", ["PROJECTS_DRILLDOWN_PROJECT", "PROJECTS_DRILLDOWN_GRANTS_DIR", "PROJECTS_DRILLDOWN_AUDIT_PATH"])
def test_a_partial_environment_refuses_to_start_it_does_not_quietly_fall_back(tmp_path, missing):
    env = {"PROJECTS_DRILLDOWN_EVENT_PATH": str(tmp_path / "ev.jsonl"), "PROJECTS_DRILLDOWN_PROJECT": SCOPE,
           "PROJECTS_DRILLDOWN_GRANTS_DIR": str(tmp_path / "perm"), "PROJECTS_DRILLDOWN_AUDIT_PATH": str(tmp_path / "audit.jsonl")}
    env.pop(missing)
    with pytest.raises(DrilldownConfigError) as e:
        drilldown_from_env(env)
    assert missing in str(e.value)


def test_env_refuses_a_memory_backend_and_a_bad_project_id(tmp_path):
    base = {"PROJECTS_DRILLDOWN_EVENT_PATH": str(tmp_path / "ev.jsonl"), "PROJECTS_DRILLDOWN_PROJECT": SCOPE,
            "PROJECTS_DRILLDOWN_GRANTS_DIR": str(tmp_path / "perm"), "PROJECTS_DRILLDOWN_AUDIT_PATH": str(tmp_path / "audit.jsonl")}
    with pytest.raises(DrilldownConfigError):
        drilldown_from_env({**base, "PROJECTS_DRILLDOWN_EVENT_BACKEND": "memory"})
    with pytest.raises(DrilldownConfigError):
        drilldown_from_env({**base, "PROJECTS_DRILLDOWN_PROJECT": "Bad Project!"})


def test_create_app_reads_the_environment_when_no_config_is_injected(tmp_path, monkeypatch):
    set_env(monkeypatch)
    log = str(tmp_path / "ev.jsonl")
    completed_mission(log)
    monkeypatch.setenv("PROJECTS_DRILLDOWN_EVENT_PATH", log)
    monkeypatch.setenv("PROJECTS_DRILLDOWN_PROJECT", SCOPE)
    monkeypatch.setenv("PROJECTS_DRILLDOWN_GRANTS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("PROJECTS_DRILLDOWN_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    cap = LocalCapability(port=PORT)
    c = TestClient(create_app(SampleProjectionProvider(), capability=cap), base_url=BASE_URL)
    _login(c, cap)
    assert c.get("/api/ui-config").json()["data_source"] == "runtime"
    monkeypatch.delenv("PROJECTS_DRILLDOWN_EVENT_PATH")
    monkeypatch.delenv("PROJECTS_DRILLDOWN_PROJECT")
    monkeypatch.delenv("PROJECTS_DRILLDOWN_GRANTS_DIR")
    monkeypatch.delenv("PROJECTS_DRILLDOWN_AUDIT_PATH")
    cap2 = LocalCapability(port=PORT)
    c2 = TestClient(create_app(SampleProjectionProvider(), capability=cap2), base_url=BASE_URL)
    _login(c2, cap2)
    assert c2.get("/api/ui-config").json()["data_source"] == "sample"
