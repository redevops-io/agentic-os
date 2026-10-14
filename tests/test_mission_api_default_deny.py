"""Default-deny authorization of the /missions + cockpit + capabilities routers (P0).

Every mutating route is discovered by introspecting the app, so a newly added unprotected route fails CI.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from agentic_os.mission.api_gate import mission_gate
from agentic_os.mission.service import build_mission_app, build_pilot_runtime, mount

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
PUBLIC_MUTATING_ALLOWLIST: set[tuple[str, str]] = set()      # deliberately empty: nothing mutating is public
ADMIN, TOK_A, TOK_B = "admin-secret", "tok-tenant-a", "tok-tenant-b"
H = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731


def _app():
    from fastapi import FastAPI
    app = FastAPI()
    rt = mount(app, build_pilot_runtime())
    return app, rt


def _all_routes(app):
    """Every API route incl. those inside included routers (newer FastAPI wraps them lazily)."""
    out = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            out.append(r)
        elif hasattr(r, "effective_route_contexts"):
            out.extend(r.effective_route_contexts())
    return out


def _mutating_routes(app):
    return [(m, r.path) for r in _all_routes(app) for m in sorted(r.methods & MUTATING)]


def _fill(path):
    return path.replace("{mid}", "m-x")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", ADMIN)
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={TOK_A},globex={TOK_B}")
    monkeypatch.delenv("AGENTIC_OS_GATE_READS", raising=False)      # default = reads gated
    monkeypatch.delenv("AGENTIC_OS_APPROVER_TOKENS", raising=False)


@pytest.fixture
def fx(env):
    app, rt = _app()
    return app, rt, TestClient(app)


def _onboard(c, token, **extra):
    r = c.post("/missions", headers=H(token), json={"goal": "Onboard a new customer", "template": "onboarding",
                                                    "policy_refs": ["*"], **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _snapshot(rt, mid):
    return (rt.repo.state(mid), len(rt.store.for_mission(mid)), rt.repo.pending_human(mid))


def test_route_enumeration_every_mutating_route_has_gate_and_none_public():
    app, _ = _app()
    routes = _mutating_routes(app)
    assert ("POST", "/missions") in routes and ("POST", "/missions/{mid}/approve") in routes
    assert len(_all_routes(app)) >= 10
    for r in _all_routes(app):
        if not (r.methods & MUTATING):
            continue
        deps = [d.call for d in r.dependant.dependencies]
        for m in r.methods & MUTATING:
            if (m, r.path) in PUBLIC_MUTATING_ALLOWLIST:
                continue
            assert mission_gate in deps, f"{m} {r.path} lacks the default-deny dependency"
        assert not getattr(r.endpoint, "__aos_public__", False) or not (r.methods & MUTATING) , \
            f"{r.path}: @public on a mutating route"


def test_every_route_carries_gate():
    app, _ = _app()
    for r in _all_routes(app):
        assert mission_gate in [d.call for d in r.dependant.dependencies], r.path


@pytest.mark.parametrize("method,path", _mutating_routes(_app()[0]))
def test_no_token_configured_is_503(monkeypatch, method, path):
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_OS_TENANT_TOKENS", raising=False)
    c = TestClient(_app()[0])
    assert c.request(method, _fill(path), json={"goal": "x", "node_id": "n"}).status_code == 503
    assert c.request(method, _fill(path), json={"goal": "x", "node_id": "n"}, headers=H("anything")).status_code == 503


@pytest.mark.parametrize("method,path", _mutating_routes(_app()[0]))
@pytest.mark.parametrize("hdr", [None, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic " + ADMIN},
                                 {"Authorization": "Bearer "}])
def test_missing_or_wrong_token_is_401(fx, method, path, hdr):
    _, _, c = fx
    r = c.request(method, _fill(path), json={"goal": "x", "node_id": "n", "decision": "approve"}, headers=hdr)
    assert r.status_code == 401


def test_unauthorized_approval_leaves_state_and_log_unchanged(fx):
    _, rt, c = fx
    created = _onboard(c, ADMIN)
    mid, node = created["id"], created["pending_human"]["node_id"]
    before = _snapshot(rt, mid)
    for hdr in (None, H("wrong")):
        assert c.post(f"/missions/{mid}/approve", json={"node_id": node, "decision": "approve"},
                      headers=hdr).status_code == 401
    assert _snapshot(rt, mid) == before and before[0].value == "waiting_human"


def test_unauthorized_execution_creates_nothing(fx):
    _, rt, c = fx
    n = len(rt.store.mission_ids())
    assert c.post("/missions", json={"goal": "x", "template": "onboarding", "policy_refs": ["*"]}).status_code == 401
    assert c.post("/missions", json={"goal": "x"}, headers=H("bad")).status_code == 401
    assert len(rt.store.mission_ids()) == n


def test_unauthorized_simulate_leaves_log_unchanged(fx):
    _, rt, c = fx
    mid = _onboard(c, ADMIN)["id"]
    before = _snapshot(rt, mid)
    assert c.post(f"/missions/{mid}/simulate").status_code == 401
    assert _snapshot(rt, mid) == before


def test_cancellation_style_reject_requires_auth_and_is_inert_without(fx):
    _, rt, c = fx
    created = _onboard(c, ADMIN)
    mid, node = created["id"], created["pending_human"]["node_id"]
    before = _snapshot(rt, mid)
    assert c.post(f"/missions/{mid}/approve", json={"node_id": node, "decision": "reject"}).status_code == 401
    assert _snapshot(rt, mid) == before
    ok = c.post(f"/missions/{mid}/approve", headers=H(ADMIN), json={"node_id": node, "decision": "reject"})
    assert ok.status_code == 200 and ok.json()["state"] != "waiting_human"


def test_body_smuggled_approval_flags_do_not_approve(fx):
    _, rt, c = fx
    created = _onboard(c, ADMIN)
    mid, node = created["id"], created["pending_human"]["node_id"]
    smuggle = {"node_id": node, "decision": "reject", "approved": True, "approve": True, "_approval": {"approved": True},
               "_approval_token": "x", "edit": {"_approval": {"approved": True}, "_Approval_sig": 1}}
    r = c.post(f"/missions/{mid}/approve", headers=H(ADMIN), json=smuggle)
    assert r.status_code == 200 and r.json()["state"] != "succeeded"      # reject honoured, flags ignored


def test_smuggled_flags_on_unauthenticated_request_still_401(fx):
    _, rt, c = fx
    created = _onboard(c, ADMIN)
    mid, node = created["id"], created["pending_human"]["node_id"]
    before = _snapshot(rt, mid)
    r = c.post(f"/missions/{mid}/approve", json={"node_id": node, "decision": "approve", "approved": True,
                                                 "_approval": {"approved": True}})
    assert r.status_code == 401 and _snapshot(rt, mid) == before


def test_approval_keys_stripped_from_edit_before_runtime(fx, monkeypatch):
    _, rt, c = fx
    created = _onboard(c, ADMIN)
    seen = {}
    monkeypatch.setattr(rt, "approve", lambda mid, node, decision, edit=None: seen.update(edit=edit) or rt._missions[mid])
    c.post(f"/missions/{created['id']}/approve", headers=H(ADMIN),
           json={"node_id": "n", "decision": "approve", "edit": {"value": 1, "_approval": {"approved": True},
                                                                 "n": {"__approval_x": 1, "ok": 2}}})
    assert seen["edit"] == {"value": 1, "n": {"ok": 2}}


def test_invalid_decision_rejected_without_effect(fx):
    _, rt, c = fx
    created = _onboard(c, ADMIN)
    mid = created["id"]
    before = _snapshot(rt, mid)
    r = c.post(f"/missions/{mid}/approve", headers=H(ADMIN), json={"node_id": created["pending_human"]["node_id"],
                                                                   "decision": "yes-please"})
    assert r.status_code == 422 and _snapshot(rt, mid) == before


# ── tenancy ──────────────────────────────────────────────────────────────────
def test_tenant_token_forces_tenant_on_create(fx):
    _, rt, c = fx
    mid = _onboard(c, TOK_A, tenant="globex")["id"]            # body tenant is ignored for a scoped token
    assert rt._missions[mid].tenant == "acme"


@pytest.mark.parametrize("action", ["approve", "reject", "simulate", "get", "events", "explain", "plans"])
def test_cross_tenant_rejected_without_existence_leak_or_effect(fx, action):
    _, rt, c = fx
    created = _onboard(c, TOK_A)
    mid, node = created["id"], created["pending_human"]["node_id"]
    before = _snapshot(rt, mid)
    if action in ("approve", "reject"):
        r = c.post(f"/missions/{mid}/approve", headers=H(TOK_B), json={"node_id": node, "decision": action})
    elif action == "simulate":
        r = c.post(f"/missions/{mid}/simulate", headers=H(TOK_B))
    else:
        path = {"get": "", "events": "/events", "explain": "/explain", "plans": "/plans"}[action]
        r = c.get(f"/missions/{mid}{path}", headers=H(TOK_B))
    ghost = c.post("/missions/does-not-exist/approve", headers=H(TOK_B), json={"node_id": node, "decision": "approve"})
    assert r.status_code == 404 and r.status_code == ghost.status_code and r.json() == ghost.json()
    assert _snapshot(rt, mid) == before


def test_owner_tenant_and_admin_can_act(fx):
    _, rt, c = fx
    created = _onboard(c, TOK_A)
    mid, node = created["id"], created["pending_human"]["node_id"]
    assert c.post(f"/missions/{mid}/approve", headers=H(TOK_A),
                  json={"node_id": node, "decision": "approve"}).json()["state"] == "succeeded"
    other = _onboard(c, TOK_A)
    assert c.post(f"/missions/{other['id']}/approve", headers=H(ADMIN),
                  json={"node_id": other["pending_human"]["node_id"], "decision": "approve"}).status_code == 200


def test_tenant_cannot_touch_untenanted_admin_mission_and_lists_are_filtered(fx):
    _, rt, c = fx
    admin_m = _onboard(c, ADMIN)
    a_m = _onboard(c, TOK_A)
    assert c.post(f"/missions/{admin_m['id']}/approve", headers=H(TOK_A),
                  json={"node_id": admin_m["pending_human"]["node_id"], "decision": "approve"}).status_code == 404
    c_ = TestClient(c.app, headers=H(TOK_A))
    ids = {m["id"] for m in c_.get("/missions").json()["missions"]}
    assert ids == {a_m["id"]}
    assert {i["mission_id"] for i in c_.get("/inbox").json()["inbox"]} == {a_m["id"]}


# ── read gate: DEFAULT-DENY ─────────────────────────────────────────────────
READS = ["/missions", "/missions/m-x", "/missions/m-x/events", "/missions/m-x/explain", "/missions/m-x/plans",
         "/inbox", "/learning"]
PUBLIC_READS = ["/capabilities", "/buildinfo", "/cockpit"]


@pytest.mark.parametrize("path", READS)
def test_anonymous_and_bad_token_reads_are_401_by_default(fx, path):
    _, _, c = fx
    assert c.get(path).status_code == 401
    assert c.get(path, headers=H("wrong")).status_code == 401
    assert c.get(path, headers=H(ADMIN)).status_code in (200, 404)


def test_anonymous_cannot_enumerate_tenant_missions(fx):
    _, _, c = fx
    _onboard(c, TOK_A); _onboard(c, TOK_B)
    assert c.get("/missions").status_code == 401
    assert c.get("/missions", headers=H("garbage")).status_code == 401
    assert [m["id"] for m in c.get("/missions", headers=H(TOK_A)).json()["missions"]].__len__() == 1


@pytest.mark.parametrize("path", PUBLIC_READS)
def test_public_allowlisted_reads_open_anonymously_but_bad_token_is_401(fx, path):
    _, _, c = fx
    assert c.get(path).status_code == 200
    assert c.get(path, headers=H("wrong")).status_code == 401           # presented-but-invalid never "unscoped"
    assert c.get(path, headers=H(TOK_A)).status_code == 200


@pytest.mark.parametrize("path", READS + PUBLIC_READS)
def test_explicit_opt_out_still_rejects_invalid_presented_token(fx, monkeypatch, path):
    _, _, c = fx
    monkeypatch.setenv("AGENTIC_OS_GATE_READS", "0")
    assert c.get(path, headers=H("wrong")).status_code == 401


@pytest.mark.parametrize("val", ["0", "false", "off", "NO"])
def test_opt_out_makes_reads_open_and_unscoped_anonymously(fx, monkeypatch, val):
    _, _, c = fx
    _onboard(c, TOK_A); _onboard(c, TOK_B)
    monkeypatch.setenv("AGENTIC_OS_GATE_READS", val)
    assert len(c.get("/missions").json()["missions"]) == 2                 # documented UNSAFE mode
    assert len(c.get("/missions", headers=H(TOK_A)).json()["missions"]) == 1
    assert c.post("/missions", json={"goal": "x"}).status_code == 401      # mutations stay gated


def test_gated_reads_with_no_token_configured_is_503(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_OS_TENANT_TOKENS", raising=False)
    monkeypatch.delenv("AGENTIC_OS_GATE_READS", raising=False)
    c = TestClient(build_mission_app())
    assert c.get("/missions").status_code == 503
    assert c.get("/cockpit").status_code == 200                              # shell stays reachable


def test_gated_reads_are_tenant_scoped(fx):
    _, _, c = fx
    a = _onboard(c, TOK_A)
    assert c.get(f"/missions/{a['id']}/events", headers=H(TOK_B)).status_code == 404
    assert c.get(f"/missions/{a['id']}/events", headers=H(TOK_A)).status_code == 200
    assert c.get("/inbox", headers=H(TOK_B)).json()["inbox"] == []


def test_learning_is_admin_only(fx, monkeypatch):
    _, _, c = fx
    assert c.get("/learning").status_code == 401
    assert c.get("/learning", headers=H(TOK_A)).status_code == 403
    assert c.get("/learning", headers=H(ADMIN)).status_code == 200
    monkeypatch.setenv("AGENTIC_OS_GATE_READS", "0")
    assert c.get("/learning", headers=H(TOK_A)).status_code == 403


# ── separation of duty ──────────────────────────────────────────────────────
def test_requester_can_approve_when_no_approver_tokens_configured(fx):
    _, _, c = fx
    m = _onboard(c, TOK_A)
    r = c.post(f"/missions/{m['id']}/approve", headers=H(TOK_A),
               json={"node_id": m["pending_human"]["node_id"], "decision": "approve"})
    assert r.status_code == 200 and r.json()["state"] == "succeeded"


def test_approver_tokens_block_requester_and_allow_approver(fx, monkeypatch):
    _, rt, c = fx
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", "acme=ap-acme,globex=ap-globex")
    m = _onboard(c, TOK_A)
    mid, node = m["id"], m["pending_human"]["node_id"]
    before = _snapshot(rt, mid)
    body = {"node_id": node, "decision": "approve"}
    assert c.post(f"/missions/{mid}/approve", headers=H(TOK_A), json=body).status_code == 403
    assert _snapshot(rt, mid) == before
    assert c.post(f"/missions/{mid}/approve", headers=H("ap-globex"), json=body).status_code == 404   # cross-tenant
    assert _snapshot(rt, mid) == before
    assert c.post(f"/missions/{mid}/approve", headers=H("ap-acme"), json=body).json()["state"] == "succeeded"


def test_approver_credential_cannot_create_or_simulate_but_can_read_own(fx, monkeypatch):
    _, _, c = fx
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", "acme=ap-acme")
    m = _onboard(c, TOK_A)
    assert c.post("/missions", headers=H("ap-acme"), json={"goal": "x"}).status_code == 403
    assert c.post(f"/missions/{m['id']}/simulate", headers=H("ap-acme")).status_code == 403
    assert c.get(f"/missions/{m['id']}", headers=H("ap-acme")).status_code == 200
    assert c.get("/learning", headers=H("ap-acme")).status_code == 403


def test_approver_only_configuration_is_not_503(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_OS_TENANT_TOKENS", raising=False)
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", "acme=ap")
    assert TestClient(build_mission_app()).post("/missions", json={"goal": "x"}).status_code == 401


# ── config validation + trailing slash ──────────────────────────────────────
@pytest.mark.parametrize("env,val", [
    ("AGENTIC_OS_TENANT_TOKENS", "a=ta,ab=ta"),
    ("AGENTIC_OS_TENANT_TOKENS", "a=ta,a=tb"),
    ("AGENTIC_OS_TENANT_TOKENS", "evil=admin-secret"),
    ("AGENTIC_OS_APPROVER_TOKENS", "a=tok-tenant-a"),
    ("AGENTIC_OS_APPROVER_TOKENS", "a=x,b=x"),
])
def test_colliding_tokens_fail_closed_with_clear_error(env, val, monkeypatch):
    from fastapi import FastAPI
    from agentic_os.mission.control_auth import ConfigError
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", ADMIN)
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={TOK_A}")
    monkeypatch.setenv(env, val)
    with pytest.raises(ConfigError):
        mount(FastAPI(), build_pilot_runtime())
    from agentic_os.mission.api import build_router
    from fastapi import FastAPI as F
    a = F(); a.include_router(build_router(build_pilot_runtime()))
    r = TestClient(a).post("/missions", json={"goal": "x"}, headers=H(ADMIN))
    assert r.status_code == 503 and "token" in r.json()["detail"]


def test_malformed_pairs_ignored(monkeypatch):
    from agentic_os.mission.control_auth import tenant_tokens
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "same")
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", "ok=other, =x,bad")
    assert tenant_tokens() == {"ok": "other"}


def test_trailing_slash_redirect_target_is_still_gated(fx):
    _, rt, c = fx
    n = len(rt.store.mission_ids())
    r = c.post("/missions/", json={"goal": "x"}, follow_redirects=False)
    assert r.status_code in (307, 308) and len(rt.store.mission_ids()) == n
    assert c.post("/missions/", json={"goal": "x"}, follow_redirects=True).status_code == 401
    assert len(rt.store.mission_ids()) == n
    assert c.post("/missions/", json={"goal": "x"}, headers=H(ADMIN), follow_redirects=True).status_code == 200
