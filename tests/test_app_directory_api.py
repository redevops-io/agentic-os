"""Projects API: /api/app-directory (list, record link, ref link, receipt links) behind the local capability gate.

The gate is unchanged: every route needs a session, writes need the CSRF header, no CORS, no cookies. The
directory adds only read-style link production: it never redirects, proxies or forwards a credential.
"""
from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from agentic_os.app_directory import load_directory
from agentic_os.local_capability import LocalCapability
from agentic_os.overlays import LocalIdentity, Principal
from agentic_os.projects_api import SampleProjectionProvider, create_app, mounted_app

PORT = 8787
BASE_URL = f"http://127.0.0.1:{PORT}"

CONFIG = {
    "policy": {"allowed_schemes": ["https"]},
    "apps": [
        {"id": "support", "display_name": "Support", "ui": "public-to-user", "auth": "app-login",
         "ui_url": "https://support.example.com/app",
         "records": {"conversation": {"label": "Conversation", "template": "/app/c/{conversation_id}",
                                      "params": {"conversation_id": "^[0-9]{1,12}$"},
                                      "ref_pattern": "^conversation:(?P<conversation_id>[0-9]+)$"}},
         "inspect": {"template": "/app/executions/{mission_id}", "params": {"mission_id": "^[A-Za-z0-9_-]{1,64}$"}},
         "bindings": {"support.resolve": {"kind": "conversation", "params": {"conversation_id": "result.conversation_id"}}}},
        {"id": "books", "display_name": "Books", "ui": "public-to-user", "auth": "sso", "ui_url": "https://books.example.com/"},
        {"id": "infra", "display_name": "Infra", "ui": "none"},
    ],
}


class Ceiling:
    def __init__(self, *perms):
        self.perms = set(perms)

    def allows(self, principal, permission):
        return permission in self.perms


class Receipts(SampleProjectionProvider):
    """A provider that exposes a runtime receipt for a mission (the optional ``mission_receipt`` hook)."""
    def mission_receipt(self, mission_id):
        if mission_id != "m-1":
            return {}
        return {"mission_id": "m-1", "steps": [{"node_id": "n1", "capability": "support.resolve",
                                                 "result": {"conversation_id": 42}}]}


def make(perms=("apps.support.open", "apps.support.records.conversation", "apps.support.inspect", "apps.books.open"),
         *, base="", config=CONFIG, provider=None, resolver=None, gate=True):
    cap = LocalCapability(port=PORT) if gate else None
    kw = dict(app_directory=load_directory(copy.deepcopy(config)), ceiling=Ceiling(*perms))
    if resolver is not None:
        kw["principal_resolver"] = resolver
    if gate:
        kw["capability"] = cap
    app = mounted_app(base, provider or Receipts(), **kw)
    c = TestClient(app, base_url=BASE_URL)
    return c, cap


def login(c, cap, base=""):
    code = cap.issue_launch_code()
    r = c.post(base + "/api/local/session", json={"code": code})
    assert r.status_code == 200, r.text
    j = r.json()
    c.headers["X-Agentic-Session"] = j["session_token"]
    return j["csrf_token"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AGENT_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_OS_APP_DIRECTORY", raising=False)


# ───────────────────────── authentication boundary (the gate is unchanged) ─────────────────────────
def test_unauthenticated_requests_are_refused_and_reveal_nothing():
    c, _cap = make()
    for method, path in (("get", "/api/app-directory"), ("post", "/api/app-directory/record-link"),
                         ("post", "/api/app-directory/ref-link"), ("get", "/api/app-directory/receipts/m-1")):
        r = getattr(c, method)(path)
        assert r.status_code in (401, 403), (path, r.status_code)
        assert "support" not in r.text.lower() and "example.com" not in r.text


def test_a_wrong_or_foreign_session_is_refused():
    c, cap = make()
    login(c, cap)
    c.headers["X-Agentic-Session"] = "not-a-session-token"
    assert c.get("/api/app-directory").status_code == 401
    other, cap2 = make()
    login(other, cap2)
    c.headers["X-Agentic-Session"] = other.headers["X-Agentic-Session"]       # a session of ANOTHER sidecar instance
    assert c.get("/api/app-directory").status_code == 401


def test_writes_need_the_csrf_header():
    c, cap = make()
    csrf = login(c, cap)
    body = {"app": "support", "kind": "conversation", "params": {"conversation_id": "42"}}
    assert c.post("/api/app-directory/record-link", json=body).status_code == 403
    assert c.post("/api/app-directory/record-link", json=body, headers={"X-Agentic-CSRF": csrf}).status_code == 200


def test_cross_origin_and_cors_are_refused():
    c, cap = make()
    login(c, cap)
    r = c.get("/api/app-directory", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.get("/api/app-directory")
    assert not [k for k in r.headers if k.lower().startswith("access-control-")]
    assert "set-cookie" not in r.headers and r.headers["cache-control"] == "no-store"


# ───────────────────────── listing ─────────────────────────
def test_listing_is_computed_from_the_ceiling():
    c, cap = make()
    login(c, cap)
    j = c.get("/api/app-directory").json()
    assert [a["id"] for a in j["apps"]] == ["support", "books"]
    assert j["apps"][0]["open"]["url"] == "https://support.example.com/app"
    assert "infra" not in json.dumps(j)


def test_a_user_with_no_grants_sees_an_empty_directory():
    c, cap = make(perms=())
    login(c, cap)
    assert c.get("/api/app-directory").json()["apps"] == []


def test_no_request_is_ever_redirected_and_no_location_header_is_set():
    c, cap = make()
    csrf = login(c, cap)
    h = {"X-Agentic-CSRF": csrf}
    rs = [c.get("/api/app-directory", follow_redirects=False),
          c.post("/api/app-directory/record-link", json={"app": "support", "kind": "conversation", "params": {"conversation_id": "42"}}, headers=h, follow_redirects=False),
          c.post("/api/app-directory/ref-link", json={"ref": "support:conversation:42"}, headers=h, follow_redirects=False),
          c.get("/api/app-directory/receipts/m-1", follow_redirects=False)]
    for r in rs:
        assert r.status_code == 200 and "location" not in r.headers


def test_directory_responses_carry_no_internal_urls_or_credentials():
    c, cap = make()
    csrf = login(c, cap)
    texts = [c.get("/api/app-directory").text, c.get("/api/app-directory/receipts/m-1").text]
    for t in texts:
        low = t.lower()
        assert "x-agentic" not in low and csrf not in t and "launch" not in low
        assert "8201" not in t and "localhost" not in low and "127.0.0.1" not in t


def test_the_principal_comes_from_the_local_identity_under_the_gate(monkeypatch):
    import agentic_os.overlays as ov
    seen = {}

    class Id(LocalIdentity):
        def authenticate(self, token):
            return Principal("local-user", "user", ())

    class Spy(Ceiling):
        def allows(self, principal, permission):
            seen.setdefault("who", set()).add(principal.id)
            return super().allows(principal, permission)
    monkeypatch.setattr(ov, "_IDENTITY", Id())
    cap = LocalCapability(port=PORT)
    app = mounted_app("", Receipts(), capability=cap, app_directory=load_directory(copy.deepcopy(CONFIG)), ceiling=Spy("apps.books.open"))
    c = TestClient(app, base_url=BASE_URL)
    login(c, cap)
    assert [a["id"] for a in c.get("/api/app-directory").json()["apps"]] == ["books"]
    assert seen["who"] == {"local-user"}


def test_a_custom_principal_resolver_decides_who_is_asking():
    seen = []

    def resolver(request):
        seen.append(request.url.path)
        return Principal("bob", "user", ())

    class PerUser:
        def allows(self, principal, permission):
            return principal.id == "bob" and permission == "apps.books.open"
    cap = LocalCapability(port=PORT)
    app = mounted_app("", Receipts(), capability=cap, app_directory=load_directory(copy.deepcopy(CONFIG)),
                      ceiling=PerUser(), principal_resolver=resolver)
    c = TestClient(app, base_url=BASE_URL)
    login(c, cap)
    assert [a["id"] for a in c.get("/api/app-directory").json()["apps"]] == ["books"]
    assert seen


def test_without_a_principal_the_directory_fails_closed():
    c, _ = make(gate=False)                              # legacy open surface: no authenticated principal exists
    j = c.get("/api/app-directory").json()
    assert j["apps"] == [] and j["notice"]
    r = c.post("/api/app-directory/record-link", json={"app": "support", "kind": "conversation", "params": {"conversation_id": "42"}})
    assert r.status_code == 404


def test_a_resolver_returning_none_fails_closed():
    c, cap = make(resolver=lambda request: None)
    login(c, cap)
    assert c.get("/api/app-directory").json()["apps"] == []


def test_a_resolver_that_raises_fails_closed():
    def boom(request):
        raise RuntimeError("idp down")
    c, cap = make(resolver=boom)
    login(c, cap)
    assert c.get("/api/app-directory").json()["apps"] == []


def test_a_ceiling_that_raises_fails_closed():
    class Boom:
        def allows(self, principal, permission):
            raise RuntimeError("grants unreadable")
    cap = LocalCapability(port=PORT)
    app = mounted_app("", Receipts(), capability=cap, app_directory=load_directory(copy.deepcopy(CONFIG)), ceiling=Boom())
    c = TestClient(app, base_url=BASE_URL)
    login(c, cap)
    assert c.get("/api/app-directory").json()["apps"] == []


# ───────────────────────── record links ─────────────────────────
def post(c, csrf, path, body):
    return c.post(path, json=body, headers={"X-Agentic-CSRF": csrf})


def test_record_link_ok():
    c, cap = make()
    csrf = login(c, cap)
    r = post(c, csrf, "/api/app-directory/record-link", {"app": "support", "kind": "conversation", "params": {"conversation_id": 42}})
    assert r.status_code == 200
    assert r.json() == {"app": "support", "kind": "conversation", "available": True,
                        "url": "https://support.example.com/app/c/42", "label": "Conversation"}


@pytest.mark.parametrize("val", ["../../x", "1/2", "1?x", "1#x", "https://evil.example/", "//evil.example", "javascript:alert(1)", "", "%2e%2e", "1\n2", None, [], {"a": 1}])
def test_record_value_injection_is_422_and_not_echoed(val):
    c, cap = make()
    csrf = login(c, cap)
    r = post(c, csrf, "/api/app-directory/record-link", {"app": "support", "kind": "conversation", "params": {"conversation_id": val}})
    assert r.status_code == 422
    assert "evil" not in r.text and "alert" not in r.text and "passwd" not in r.text
    assert "url" not in r.json()


def test_extra_request_fields_are_refused_so_a_caller_cannot_supply_a_url_or_host():
    c, cap = make()
    csrf = login(c, cap)
    for extra in ({"url": "https://evil.example/"}, {"host": "evil.example"}, {"redirect": "https://evil.example/"}, {"next": "/x"}):
        body = {"app": "support", "kind": "conversation", "params": {"conversation_id": "42"}, **extra}
        r = post(c, csrf, "/api/app-directory/record-link", body)
        assert r.status_code == 422 and "evil" not in r.text


def test_unknown_unlisted_ui_none_and_denied_apps_give_the_same_404():
    c, cap = make(perms=("apps.books.open",))
    csrf = login(c, cap)
    bodies = set()
    for app in ("support", "nosuchapp", "infra", "../x", "SUPPORT", ""):
        r = post(c, csrf, "/api/app-directory/record-link", {"app": app, "kind": "conversation", "params": {"conversation_id": "42"}})
        assert r.status_code in (404, 422), (app, r.status_code)
        if r.status_code == 404:
            bodies.add(r.text)
    assert len(bodies) == 1
    r = post(c, csrf, "/api/app-directory/record-link", {"app": "support", "kind": "conversation", "params": {"conversation_id": "42"}})
    assert r.status_code == 404 and "support" not in r.text.lower()


def test_unsupported_record_kind_is_available_false_with_reason():
    c, cap = make()
    csrf = login(c, cap)
    r = post(c, csrf, "/api/app-directory/record-link", {"app": "books", "kind": "invoice", "params": {}})
    assert r.status_code == 200 and r.json()["available"] is False and r.json()["reason"] and "url" not in r.json()


def test_ref_link_ok_and_malformed():
    c, cap = make()
    csrf = login(c, cap)
    r = post(c, csrf, "/api/app-directory/ref-link", {"ref": "support:conversation:42"})
    assert r.status_code == 200 and r.json()["url"] == "https://support.example.com/app/c/42"
    r = post(c, csrf, "/api/app-directory/ref-link", {"ref": "support:conversation:42/../../x"})
    assert r.status_code == 200 and r.json()["available"] is False and "url" not in r.json()
    r = post(c, csrf, "/api/app-directory/ref-link", {"ref": "ghost:conversation:42"})
    assert r.status_code == 404


def test_oversized_bodies_are_refused():
    c, cap = make()
    csrf = login(c, cap)
    r = post(c, csrf, "/api/app-directory/ref-link", {"ref": "support:conversation:" + "9" * 100000})
    assert r.status_code in (413, 422)


# ───────────────────────── receipts ─────────────────────────
def test_receipt_links_come_from_the_server_side_receipt_not_the_client():
    c, cap = make()
    login(c, cap)
    j = c.get("/api/app-directory/receipts/m-1").json()
    assert j["records"][0]["url"] == "https://support.example.com/app/c/42"
    assert j["inspect"][0]["url"] == "https://support.example.com/app/executions/m-1"


def test_receipt_links_respect_the_ceiling():
    c, cap = make(perms=("apps.books.open",))
    login(c, cap)
    j = c.get("/api/app-directory/receipts/m-1").json()
    assert j["records"] == [] and j["inspect"] == [] and "support" not in json.dumps(j)


def test_receipt_for_an_unknown_mission_and_traversal_ids_are_404():
    c, cap = make()
    login(c, cap)
    for mid in ("nope", "..%2f..%2fetc", "m-1%2f..", "m%0a1", "x" * 300):
        r = c.get(f"/api/app-directory/receipts/{mid}")
        assert r.status_code == 404, (mid, r.status_code)


def test_a_provider_without_receipts_yields_an_honest_empty_answer():
    c, cap = make(provider=SampleProjectionProvider())
    login(c, cap)
    j = c.get("/api/app-directory/receipts/4821").json()
    assert j["records"] == [] and j["inspect"] == [] and j["notice"]


# ───────────────────────── other surfaces unchanged ─────────────────────────
def test_the_existing_apps_projection_is_unchanged():
    c, cap = make()
    login(c, cap)
    apps = c.get("/api/projects/customer-ops/apps").json()
    assert {a["provider"] for a in apps} >= {"hubspot", "slack", "stripe"}
    assert all("ui_url" not in a and "open" not in a for a in apps)


def test_openapi_still_generates_under_the_gate():
    c, cap = make()
    login(c, cap)
    r = c.get("/openapi.json")
    assert r.status_code == 200 and "/api/app-directory" in r.json()["paths"]


def test_works_under_a_base_path():
    c, cap = make(base="/projects")
    login(c, cap, "/projects")
    assert [a["id"] for a in c.get("/projects/api/app-directory").json()["apps"]] == ["support", "books"]
    assert c.get("/api/app-directory").status_code in (401, 404)


def test_default_directory_comes_from_the_environment_and_is_empty_when_unset(tmp_path, monkeypatch):
    cap = LocalCapability(port=PORT)
    c = TestClient(mounted_app("", Receipts(), capability=cap, ceiling=Ceiling("apps.books.open")), base_url=BASE_URL)
    login(c, cap)
    assert c.get("/api/app-directory").json()["apps"] == []
    p = tmp_path / "dir.json"
    p.write_text(json.dumps(CONFIG))
    monkeypatch.setenv("AGENTIC_OS_APP_DIRECTORY", str(p))
    cap2 = LocalCapability(port=PORT)
    c2 = TestClient(mounted_app("", Receipts(), capability=cap2, ceiling=Ceiling("apps.books.open")), base_url=BASE_URL)
    login(c2, cap2)
    assert [a["id"] for a in c2.get("/api/app-directory").json()["apps"]] == ["books"]


def test_csp_and_security_headers_on_the_ui_shell_are_unchanged_by_the_directory():
    c, cap = make()
    with_dir = c.get("/").headers
    plain = TestClient(mounted_app("", SampleProjectionProvider(), capability=LocalCapability(port=PORT)), base_url=BASE_URL).get("/").headers
    for h in ("content-security-policy", "x-frame-options", "referrer-policy", "x-content-type-options"):
        assert with_dir.get(h) == plain.get(h)
