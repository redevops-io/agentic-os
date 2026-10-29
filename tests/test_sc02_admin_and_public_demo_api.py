"""SC-02 (design decision): two distinct interfaces.

ADMIN API (control plane and the showcase's admin routes): grant list, status, preview, authorize/diagnostics, create, update, remove ALL
require an admin credential. Fail closed: no credential, a wrong, empty or malformed one -> 401; nothing configured -> 503 (never open);
a credential in the query string -> 400. Constant-time compare. There is no public-read switch.

PUBLIC DEMO API (showcase only): a fixed list of SYNTHETIC scenarios. A visitor can choose a scenario id and nothing else; it never returns
a real grant, a principal, a row predicate, an authorisation diagnostic or store status.

SC-18: the pages put no dynamic value in an inline handler and escape for attribute context (checked by RUNNING the page script in node).
"""
from __future__ import annotations

import base64
import html.parser
import inspect
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap

import pytest

from agentic_os import permissions_demo as demo_engine

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = base64.b64encode(b"a" * 32).decode()
ADMIN = "adm-secret-4711-0123456789"

HARNESS = textwrap.dedent("""
    import importlib.util, json, os, sys
    from fastapi.testclient import TestClient
    from agentic_os import control_plane as cp
    spec = importlib.util.spec_from_file_location("d", "deploy/permissions-showcase-demo/app.py")
    demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(demo)
    ADMIN = os.environ.get("TEST_ADMIN", "")
    GOOD = {"x-api-key": ADMIN}
    GRANT = {"subject_kind": "role", "subject": "clerk", "resource_kind": "table", "resource_name": "crm.customers"}
    ROUTES = [("GET", "/api/permissions/status", None), ("GET", "/api/permissions/grants", None),
              ("POST", "/api/permissions/preview", {"subject_kind": "app", "subject": "support"}),
              ("POST", "/api/permissions/authorize", {"app": "support", "resource_name": "crm.customers"}),
              ("POST", "/api/permissions/grants", GRANT), ("PUT", "/api/permissions/grants/g-" + "0" * 32, {"row_values": ["x"]}),
              ("DELETE", "/api/permissions/grants/g-" + "0" * 32, None)]
    CREDS = {"none": {}, "wrong": {"x-api-key": "nope"}, "empty": {"x-api-key": ""}, "empty-bearer": {"authorization": "Bearer "},
             "bearer-wrong": {"authorization": "Bearer nope"}, "basic": {"authorization": "Basic " + __import__("base64").b64encode(
                 (":" + ADMIN).encode()).decode()}, "good": GOOD}
    out = {}
    for name, app in (("cp", cp.app), ("demo", demo.app)):
        c = TestClient(app, raise_server_exceptions=False)
        res = {}
        for method, url, body in ROUTES:
            for cname, hdr in CREDS.items():
                r = c.request(method, url, json=body, headers=hdr)
                res[f"{method} {url} [{cname}]"] = r.status_code
            for q in ("?api_key=" + ADMIN, "?token=" + ADMIN, "?key=" + ADMIN, "?access_token=" + ADMIN, "?X-API-Key=" + ADMIN):
                res[f"{method} {url} [query {q.split('=')[0][1:]}]"] = c.request(method, url + q, json=body).status_code
                res[f"{method} {url} [query+header {q.split('=')[0][1:]}]"] = c.request(method, url + q, json=body, headers=GOOD).status_code
        out[name] = res
    print(json.dumps(out))
""")


def _run(code, tmp_path, **env_extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_", "TEST_", "DEMO_"))}
    env.update(PYTHONPATH=str(ROOT), PERMISSIONS_KEY=KEY, PERMISSIONS_DIR=str(tmp_path / "p"), **env_extra)
    p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    assert p.returncode == 0, p.stderr[-800:]
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def configured(tmp_path_factory):
    return _run(HARNESS, tmp_path_factory.mktemp("cfg"), PERMISSIONS_ADMIN_KEY=ADMIN, TEST_ADMIN=ADMIN)


@pytest.fixture(scope="module")
def unconfigured(tmp_path_factory):
    return _run(HARNESS, tmp_path_factory.mktemp("nocfg"), TEST_ADMIN=ADMIN)


@pytest.fixture(scope="module")
def public_read_switches(tmp_path_factory):
    return _run(HARNESS, tmp_path_factory.mktemp("pr"), PERMISSIONS_ADMIN_KEY=ADMIN, TEST_ADMIN=ADMIN, PERMISSIONS_PUBLIC_READ="1",
                PUBLIC_READ="1", DEMO_PUBLIC_READ="1", DEMO_READ_ONLY="1")


PLANES = ("cp", "demo")
ROUTE_KEYS = [f"{m} {u}" for m, u in (("GET", "/api/permissions/status"), ("GET", "/api/permissions/grants"), ("POST", "/api/permissions/preview"),
                                      ("POST", "/api/permissions/authorize"), ("POST", "/api/permissions/grants"),
                                      ("PUT", "/api/permissions/grants/g-" + "0" * 32), ("DELETE", "/api/permissions/grants/g-" + "0" * 32))]


class TestAdminApiRequiresAuthenticatedAuthorisationOnEveryRoute:
    @pytest.mark.parametrize("plane", PLANES)
    @pytest.mark.parametrize("cred", ["none", "wrong", "empty", "empty-bearer", "bearer-wrong", "basic"])
    def test_every_route_denies_a_missing_wrong_empty_or_malformed_credential(self, configured, plane, cred):
        for r in ROUTE_KEYS:
            assert configured[plane][f"{r} [{cred}]"] == 401, (plane, r, cred)

    @pytest.mark.parametrize("plane", PLANES)
    def test_the_correct_credential_is_accepted_on_every_route(self, configured, plane):
        assert [configured[plane][f"{r} [good]"] for r in ROUTE_KEYS] == [200, 200, 200, 200, 200, 404, 200]

    @pytest.mark.parametrize("plane", PLANES)
    @pytest.mark.parametrize("name", ["api_key", "token", "key", "access_token", "X-API-Key"])
    def test_a_credential_in_the_query_string_is_refused_with_and_without_the_header(self, configured, plane, name):
        for r in ROUTE_KEYS:
            assert configured[plane][f"{r} [query {name}]"] == 400 and configured[plane][f"{r} [query+header {name}]"] == 400, (plane, r, name)

    @pytest.mark.parametrize("plane", PLANES)
    def test_with_no_admin_key_configured_every_route_is_disabled_never_open(self, unconfigured, plane):
        for r in ROUTE_KEYS:
            for cred in ("none", "wrong", "empty", "good"):
                assert unconfigured[plane][f"{r} [{cred}]"] == 503, (plane, r, cred)

    @pytest.mark.parametrize("plane", PLANES)
    def test_no_public_read_switch_exists_or_changes_anything(self, public_read_switches, plane):
        for r in ROUTE_KEYS:
            assert public_read_switches[plane][f"{r} [none]"] == 401 and public_read_switches[plane][f"{r} [wrong]"] == 401

    def test_the_comparison_is_constant_time_and_no_open_branch_exists_in_the_source(self):
        for f in ("agentic_os/permissions_http.py", "agentic_os/mission/control_auth.py"):
            assert "compare_digest" in (ROOT / f).read_text()
        src = (ROOT / "deploy/permissions-showcase-demo/app.py").read_text() + (ROOT / "agentic_os/permissions_http.py").read_text()
        for bad in ("public_read", "PUBLIC_READ", "if not expected:\n        return", "return True  # open"):
            assert bad not in src

    def test_every_permissions_route_of_the_control_plane_carries_the_admin_dependency(self, tmp_path):
        out = _run(textwrap.dedent("""
            import json
            from agentic_os import control_plane as cp
            routes = [r for r in cp.app.routes if getattr(r, "path", "").startswith("/api/permissions")]
            print(json.dumps({"n": len(routes), "all": all(any(d.call is cp.require_permissions_admin for d in r.dependant.dependencies)
                                                            for r in routes)}))
        """), tmp_path)
        assert out == {"n": 7, "all": True}


DEMO_HARNESS = textwrap.dedent("""
    import importlib.util, json, os
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("d", "deploy/permissions-showcase-demo/app.py")
    demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(demo)
    c = TestClient(demo.app, raise_server_exceptions=False)
    ADMIN = {"x-api-key": os.environ["TEST_ADMIN"]}
    marker = {"subject_kind": "role", "subject": "zz-marker-subject-9", "resource_kind": "table", "resource_name": "zz.marker.resource"}
    added = c.post("/api/permissions/grants", json=marker, headers=ADMIN).json()
    ids = [s["id"] for s in c.get("/api/demo/scenarios").json()]
    pub = {"list": c.get("/api/demo/scenarios").text, "health": c.get("/healthz").text, "page": c.get("/permissions").text}
    cookies, bodies = [], {}
    for i in ids:
        r = c.get("/api/demo/scenarios/" + i); pub[i] = r.text; bodies[i] = r.json()
        cookies.append(r.headers.get("set-cookie"))
        # nothing the visitor adds to the URL changes the answer
        r2 = c.get("/api/demo/scenarios/" + i + "?roles=admin&role=admin&app=x&user=y&resource_name=hr.salaries&action=write&subject=zz-marker-subject-9")
        pub[i + "+q"] = r2.text
    bad_ids = ["nope", "../etc/passwd", "UPPER", "a b", "x" * 41, "%00", "support-us-masked/../x", "", "support_us", "é", "-lead", "a" * 400]
    codes = {b: c.get("/api/demo/scenarios/" + b).status_code for b in bad_ids}
    methods = {m: c.request(m, "/api/demo/scenarios/support-us-masked").status_code for m in ("POST", "PUT", "DELETE", "PATCH")}
    methods["list POST"] = c.post("/api/demo/scenarios", json={"roles": ["admin"]}).status_code
    before = sorted(g["id"] for g in c.get("/api/permissions/grants", headers=ADMIN).json())
    for _ in range(5):
        for i in ids: c.get("/api/demo/scenarios/" + i)
    after = sorted(g["id"] for g in c.get("/api/permissions/grants", headers=ADMIN).json())
    print(json.dumps({"ids": ids, "pub": pub, "cookies": cookies, "codes": codes, "methods": methods, "added_id": added["id"], "before": before,
                      "after": after}))
""")


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory):
    return _run(DEMO_HARNESS, tmp_path_factory.mktemp("demo"), PERMISSIONS_ADMIN_KEY=ADMIN, TEST_ADMIN=ADMIN)


ALLOWED_KEYS = {"id", "title", "description", "explanation", "action", "allowed", "columns", "full_count", "visible_count", "withheld", "records"}
FORBIDDEN_KEYS = {"row_filter", "rules", "reason", "key_source", "sealed", "created_by", "masked_columns", "row_values", "subject_kind", "subject",
                  "resource_kind", "plaintext_dev", "malformed", "counter", "decision", "principal", "identity", "roles", "app", "user",
                  "encrypted", "count", "grants"}
FORBIDDEN_TEXT = ("zz-marker", "zz.marker", "seed-support", "seed-finance", "key_source", "PERMISSIONS_", "grants.enc")


def _all_keys(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(x, list):
        for v in x:
            yield from _all_keys(v)


class TestPublicDemoApiIsSyntheticAndNarrow:
    def test_the_scenario_ids_are_the_fixed_allowlist(self, demo_run):
        assert demo_run["ids"] == list(demo_engine.SCENARIOS) and len(demo_run["ids"]) >= 5

    def test_a_result_carries_only_the_allowlisted_synthetic_fields(self, demo_run):
        for i in demo_run["ids"]:
            body = json.loads(demo_run["pub"][i])
            assert set(body) == ALLOWED_KEYS and body["id"] == i
            for rec in body["records"]:
                assert set(rec) <= set(body["columns"]) | {"_masked"}

    def test_nothing_real_or_diagnostic_appears_in_any_public_response(self, demo_run):
        for key, text in demo_run["pub"].items():
            if key == "page":
                continue
            assert not (set(_all_keys(json.loads(text))) & FORBIDDEN_KEYS), key
            for bad in FORBIDDEN_TEXT:
                assert bad not in text, (key, bad)

    def test_a_real_grant_added_through_the_admin_api_never_appears_publicly(self, demo_run):
        for key, text in demo_run["pub"].items():
            assert "zz-marker" not in text and "zz.marker" not in text and demo_run["added_id"] not in text, key

    def test_a_visitor_cannot_choose_an_identity_role_resource_or_action(self, demo_run):
        for i in demo_run["ids"]:
            assert demo_run["pub"][i] == demo_run["pub"][i + "+q"], i             # the query string changes nothing
        assert set(demo_run["methods"].values()) == {405}                          # and the endpoints accept no body at all

    def test_unknown_and_malformed_ids_are_404_or_400(self, demo_run):
        assert all(code in (400, 404, 405) for b, code in demo_run["codes"].items() if b) and demo_run["codes"]["nope"] == 404
        assert demo_run["codes"]["UPPER"] == 400 and demo_run["codes"]["a b"] == 400 and demo_run["codes"]["x" * 41] == 400

    def test_there_is_no_visitor_state_no_cookie_and_the_real_store_is_untouched(self, demo_run):
        assert not any(demo_run["cookies"])
        assert demo_run["before"] == demo_run["after"] and demo_run["added_id"] in demo_run["after"]

    def test_the_health_endpoint_is_liveness_only(self, demo_run):
        assert json.loads(demo_run["pub"]["health"]) == {"ok": True}

    def test_the_public_page_calls_the_demo_api_and_the_admin_api_only_inside_the_admin_block(self, demo_run):
        page = demo_run["pub"]["page"]
        assert "api/demo/scenarios" in page
        a, b = page.index("// ADMIN-BEGIN"), page.index("// ADMIN-END")
        import re
        for m in re.finditer(r"api/permissions/", page):
            assert a < m.start() < b
        assert "setInterval" not in page


class TestTheScenarioEngineItself:
    def test_the_ids_are_validated(self):
        for ok in ("support-us-masked", "a", "x" * 40):
            assert demo_engine.valid_scenario_id(ok)
        for bad in ("", "UPPER", "a b", "x" * 41, "../x", "-a", None, 5, ["a"], "a/b"):
            assert not demo_engine.valid_scenario_id(bad)
        with pytest.raises(KeyError):
            demo_engine.evaluate("nope")

    def test_every_scenario_is_deterministic_and_needs_no_environment(self, monkeypatch):
        for k in list(os.environ):
            if k.startswith(("PERMISSIONS_", "AGENTIC_OS_")):
                monkeypatch.delenv(k)
        for i in demo_engine.SCENARIOS:
            assert demo_engine.evaluate(i) == demo_engine.evaluate(i)

    def test_the_engine_never_touches_the_real_store(self):
        src = inspect.getsource(demo_engine)
        assert "GrantStore" not in src and "PERMISSIONS_DIR" not in src and "os.environ" not in src

    def test_the_scenarios_show_what_they_claim_with_exact_matching(self):
        eu = demo_engine.evaluate("analyst-own-or-eu")
        assert {r["region"] for r in eu["records"] if r["owner"] != "alice"} == {"eu"}
        per = {r["id"]: r for r in demo_engine.evaluate("masks-per-row")["records"]}
        assert per["C-1004"]["email"] is None and per["C-1001"]["email"] is not None
        assert demo_engine.evaluate("no-grant-denied")["allowed"] is False and demo_engine.evaluate("read-only-cannot-write")["allowed"] is False


# ───────────────────────── SC-18: run the page scripts against malicious data ─────────────────────────

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
EVIL_ID = 'x"><img src=x onerror=alert(1)>\');alert(2);(\''
EVIL_SUBJECT = '<script>alert(3)</script>"\' onmouseover="alert(4)'


class _Attrs(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _render(page_html, fixtures, tmp):
    (tmp / "page.html").write_text(page_html)
    (tmp / "fx.json").write_text(json.dumps(fixtures))
    p = subprocess.run(["node", str(ROOT / "tests" / "_page_js_harness.js"), str(tmp / "page.html"), str(tmp / "fx.json")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr[-500:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert "error" not in out, out
    return out


def _grant(**kw):
    return {"id": EVIL_ID, "subject_kind": "role", "subject": EVIL_SUBJECT, "resource_kind": "table", "resource_name": EVIL_SUBJECT,
            "actions": [EVIL_SUBJECT], "row_scope": "in", "row_column": EVIL_SUBJECT, "row_values": [EVIL_SUBJECT],
            "masked_columns": [EVIL_SUBJECT], **kw}


def _assert_safe(inner):
    p = _Attrs()
    p.feed(inner)
    tags = {t for t, _ in p.tags}
    assert tags <= {"table", "thead", "tbody", "tr", "th", "td", "span", "button", "p", "div"}, tags     # no script / img / event handler tags
    for tag, attrs in p.tags:
        assert not any(k.startswith("on") for k in attrs), (tag, attrs)
        if tag == "button":
            assert set(attrs) == {"class", "data-id"} and attrs["data-id"] == EVIL_ID           # the id round-trips as DATA


@needs_node
class TestPagesEscapeEveryDynamicValue:
    def test_the_admin_page_renders_a_malicious_stored_id_and_strings_as_inert_data(self, tmp_path):
        from agentic_os import permissions as P
        from agentic_os import views
        page = views.permissions_page(subjects=["a"], roles=["admin"], resource=P.PREVIEW_RESOURCE, columns=P.PREVIEW_COLS,
                                      subject_kinds=list(P.SUBJECT_KINDS), resource_kinds=list(P.RESOURCE_KINDS), actions=list(P.ACTIONS),
                                      row_scopes=list(P.ROW_SCOPES))
        assert 'onclick="delGrant' not in page and "onclick=\"delGrant('" not in page
        out = _render(page, {"__type": {"apikey": "k"}, "__press": ["unlock"], "api/permissions/grants": {"status": 200, "body": [_grant()]},
                             "api/permissions/status": {"status": 200, "body": {"encrypted": True, "key_source": "env", "count": 1}},
                             "api/permissions/preview": {"status": 200, "body": {"full": {"count": 0, "records": []}, "limited": {
                                 "count": 0, "withheld": 0, "records": [], "decision": {"allowed": False, "reason": EVIL_SUBJECT}}}}}, tmp_path)
        _assert_safe(out["els"]["grows"]["innerHTML"])
        assert "<script" not in json.dumps(out["els"]).replace("\\u003c", "<") or "&lt;script" in out["els"]["grows"]["innerHTML"]
        deletes = [c for c in out["calls"] if c["method"] == "DELETE"]
        assert len(deletes) == 1 and deletes[0]["url"].endswith("/" + __import__("urllib.parse").parse.quote(EVIL_ID, safe="-_.!~*'()"))
        assert all(c["headers"].get("X-API-Key") == "k" for c in out["calls"])          # the key rides on EVERY call, reads included

    def test_the_admin_page_without_a_key_calls_nothing_it_cannot_authenticate(self, tmp_path):
        from agentic_os import permissions as P
        from agentic_os import views
        page = views.permissions_page(subjects=["a"], roles=["admin"], resource=P.PREVIEW_RESOURCE, columns=P.PREVIEW_COLS,
                                      subject_kinds=list(P.SUBJECT_KINDS), resource_kinds=list(P.RESOURCE_KINDS), actions=list(P.ACTIONS),
                                      row_scopes=list(P.ROW_SCOPES))
        out = _render(page, {"api/permissions/grants": {"status": 401, "body": {}}, "api/permissions/status": {"status": 401, "body": {}}}, tmp_path)
        assert out["calls"] == []                                           # locked: nothing is called until a key is applied
        out = _render(page, {"__type": {"apikey": "wrong"}, "__press": ["unlock"], "api/permissions/grants": {"status": 401, "body": {}},
                             "api/permissions/status": {"status": 401, "body": {}}, "api/permissions/preview": {"status": 401, "body": {}}}, tmp_path)
        assert "admin key missing or wrong" in out["els"]["grows"]["innerHTML"]

    def test_the_demo_pages_admin_panel_renders_a_malicious_stored_id_as_inert_data(self, tmp_path):
        from agentic_os import views
        page = views.permissions_demo_page(demo_engine.list_scenarios())
        assert "onclick" not in page
        fx = {"__values": {"akey": "k", "sc": "support-us-masked"}, "__press": ["aload"],
              "api/permissions/grants": {"status": 200, "body": [_grant()]},
              "api/permissions/status": {"status": 200, "body": {"encrypted": True}},
              "api/demo/scenarios/support-us-masked": {"status": 200, "body": demo_engine.evaluate("support-us-masked")}}
        out = _render(page, fx, tmp_path)
        _assert_safe(out["els"]["abox"]["innerHTML"])
        admin_calls = [c for c in out["calls"] if "api/permissions" in c["url"]]
        assert admin_calls and all(c["headers"].get("X-API-Key") == "k" for c in admin_calls)       # key in a header, never in the URL
        assert not any("k" == c["url"].split("=")[-1] and "?" in c["url"] for c in out["calls"])
        deletes = [c for c in out["calls"] if c["method"] == "DELETE"]
        assert len(deletes) == 1 and "%22" in deletes[0]["url"] and "<" not in deletes[0]["url"]    # the id is URL-encoded

    def test_the_admin_panel_is_inert_until_a_key_is_entered(self, tmp_path):
        from agentic_os import views
        page = views.permissions_demo_page(demo_engine.list_scenarios())
        out = _render(page, {"__values": {"sc": "support-us-masked"}, "__press": ["aload"],
                             "api/demo/scenarios/support-us-masked": {"status": 200, "body": demo_engine.evaluate("support-us-masked")}}, tmp_path)
        assert not any("api/permissions" in c["url"] for c in out["calls"]) and "enter the admin key" in out["els"]["abox"]["textContent"]

    def test_the_public_page_never_calls_the_admin_api_on_load(self, tmp_path):
        from agentic_os import views
        page = views.permissions_demo_page(demo_engine.list_scenarios())
        out = _render(page, {"__values": {"sc": "support-us-masked"},
                             "api/demo/scenarios/support-us-masked": {"status": 200, "body": demo_engine.evaluate("support-us-masked")}}, tmp_path)
        assert [c["url"] for c in out["calls"]] == ["api/demo/scenarios/support-us-masked"]
        assert "withheld" in out["els"]["sres"]["innerHTML"]
