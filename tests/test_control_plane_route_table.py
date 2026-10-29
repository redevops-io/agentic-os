"""A route table that cannot drift: every route of the control plane has an explicit, expected access class, and a new route fails this test until someone classifies it.

Classes: ``fleet`` (the fleet key or the control token: ``require_api_key``), ``perm-admin`` (``PERMISSIONS_ADMIN_KEY`` only), ``mission-gate`` (the mission routers' gate: control or tenant
token, reads gated by default), ``mission-admin`` (mission gate, unscoped admin token only), ``public`` (deliberately public: liveness, catalogue, static pages, and the mission routers'
``@public`` shell/catalogue/build info) and ``open-proxy`` (the module proxy, anonymous today: public issue #528, not changed by this release).
"""
from __future__ import annotations

import pytest

FLEET, PERM, GATE, GATE_ADMIN, PUBLIC, PROXY = "fleet", "perm-admin", "mission-gate", "mission-admin", "public", "open-proxy"

EXPECTED = {
    # the fleet credential
    ("GET", "/approvals"): FLEET, ("POST", "/approvals/{approval_id}/{decision}"): FLEET, ("POST", "/dispatch"): FLEET, ("POST", "/up"): FLEET, ("POST", "/down"): FLEET,
    ("GET", "/jobs/{job_id}"): FLEET, ("GET", "/api/fleet"): FLEET, ("GET", "/status"): FLEET, ("GET", "/notify/status"): FLEET, ("GET", "/openapi.json"): FLEET,
    # the permissions admin key
    ("GET", "/api/permissions/status"): PERM, ("GET", "/api/permissions/grants"): PERM, ("POST", "/api/permissions/grants"): PERM, ("PUT", "/api/permissions/grants/{gid}"): PERM,
    ("DELETE", "/api/permissions/grants/{gid}"): PERM, ("POST", "/api/permissions/preview"): PERM, ("POST", "/api/permissions/authorize"): PERM,
    # public on purpose
    ("GET", "/"): PUBLIC, ("GET", "/health"): PUBLIC, ("GET", "/modules"): PUBLIC, ("GET", "/overview"): PUBLIC, ("GET", "/permissions"): PUBLIC,
    ("GET", "/buildinfo"): PUBLIC, ("GET", "/capabilities"): PUBLIC, ("GET", "/cockpit"): PUBLIC,
    # anonymous today: the module proxy (issue #528)
    ("GET", "/m/{name}"): PROXY, ("GET", "/m/{name}/raw"): PROXY, ("GET", "/m/{name}/api/{path:path}"): PROXY, ("POST", "/m/{name}/api/{path:path}"): PROXY,
    # the mission routers' gate
    ("GET", "/inbox"): GATE, ("GET", "/missions"): GATE, ("POST", "/missions"): GATE, ("GET", "/missions/{mid}"): GATE, ("POST", "/missions/{mid}/approve"): GATE,
    ("POST", "/missions/{mid}/compensation/{action}"): GATE, ("GET", "/missions/{mid}/events"): GATE, ("GET", "/missions/{mid}/explain"): GATE,
    ("GET", "/missions/{mid}/plans"): GATE, ("POST", "/missions/{mid}/reconcile"): GATE, ("POST", "/missions/{mid}/simulate"): GATE,
    ("GET", "/learning"): GATE_ADMIN,
}


@pytest.fixture()
def cp(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("cp-routes")
    monkeypatch.setenv("PERMISSIONS_DIR", str(d / "perm"))
    monkeypatch.setenv("AGENTIC_OS_HOME", str(d / "home"))
    from agentic_os import control_plane
    return control_plane


def _classify(route, router_gated: bool) -> str:
    deps = {getattr(d.dependency, "__name__", "") for d in (getattr(route, "dependencies", None) or [])}
    ep = getattr(route, "endpoint", None)
    if "require_api_key" in deps:
        return FLEET
    if "require_permissions_admin" in deps:
        return PERM
    if router_gated:
        if getattr(ep, "__aos_public__", False):
            return PUBLIC
        return GATE_ADMIN if getattr(ep, "__aos_admin_only__", False) else GATE
    return PROXY if getattr(route, "path", "").startswith("/m/") else PUBLIC


def _actual(app):
    rows = {}
    for r in app.routes:
        if type(r).__name__ == "_IncludedRouter":
            for s in r.original_router.routes:
                for m in sorted(s.methods or []):
                    rows[(m, s.path)] = _classify(s, True)
        elif hasattr(r, "methods") and r.methods:
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                rows[(m, r.path)] = _classify(r, False)
    return rows


def test_every_route_has_an_expected_access_class_and_no_route_is_unclassified(cp):
    actual = _actual(cp.app)
    assert set(actual) == set(EXPECTED), (
        f"unclassified (new) routes: {sorted(set(actual) - set(EXPECTED))}; expected but gone: {sorted(set(EXPECTED) - set(actual))}. "
        "Classify a new route here, and give it a dependency unless it is public on purpose.")
    wrong = {k: (actual[k], EXPECTED[k]) for k in actual if actual[k] != EXPECTED[k]}
    assert wrong == {}, wrong


def test_the_mission_routers_carry_the_gate_and_public_is_only_explicit(cp):
    for r in cp.app.routes:
        if type(r).__name__ == "_IncludedRouter":
            for s in r.original_router.routes:
                if getattr(s, "endpoint", None) is None:
                    continue
                gated = any(getattr(d.dependency, "__name__", "") == "mission_gate" for d in (getattr(s, "dependencies", None) or [])) or any(
                    getattr(d.dependency, "__name__", "") == "mission_gate" for d in (getattr(r.original_router, "dependencies", None) or []))
                assert gated, f"{s.path} is on a mission router without the mission gate"


def test_no_get_route_other_than_the_listed_public_ones_is_open(cp):
    open_gets = sorted(p for (m, p), c in _actual(cp.app).items() if m == "GET" and c in (PUBLIC, PROXY))
    assert open_gets == sorted(["/", "/health", "/modules", "/overview", "/permissions", "/buildinfo", "/capabilities", "/cockpit", "/m/{name}", "/m/{name}/raw", "/m/{name}/api/{path:path}"])


def test_every_fleet_class_route_refuses_an_anonymous_call_when_nothing_is_configured(cp, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("AGENTIC_OS_API_KEY", "AGENTIC_OS_CONTROL_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    client = TestClient(cp.app, follow_redirects=False)
    for (method, path), cls in EXPECTED.items():
        if cls != FLEET:
            continue
        url = path.replace("{approval_id}", "a").replace("{decision}", "approve").replace("{job_id}", "job-1")
        r = client.request(method, url)
        assert r.status_code in (503, 422), (method, path, r.status_code)       # 422: a body-less POST fails validation first, with no state touched
