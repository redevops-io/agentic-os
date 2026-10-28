"""Public permissions pages (focused review PP-02, PP-04, PP-05, PP-07, PP-08, PP-10, PP-11, PP-12):

* both pages carry a strict CSP (per-response nonce, default-src 'none', connect-src 'self', frame-ancestors 'none', ...) and the other security
  headers; the admin API answers with no-store;
* the pages have no inline event handler and no style attribute, so the CSP needs no 'unsafe-inline';
* a trailing-slash redirect is relative (it never echoes the Host header or an http scheme); the showcase answers 404;
* /docs, /redoc and /openapi.json are off on the showcase and unauthenticated on the control plane;
* the 503 body names no environment variable; the fix is logged server-side;
* failed admin attempts are throttled per client with bounded memory; a trusted proxy setting is the only way X-Forwarded-For is believed;
* the showcase starts and serves the synthetic API with NO PERMISSIONS_KEY and never builds a real GrantStore without a usable admin key;
* the wording claims only what is true.
All apps are run in a subprocess (fresh import state) with a TestClient: no network.
"""
from __future__ import annotations

import base64
import html.parser
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = base64.b64encode(b"p" * 32).decode()
ADMIN = "pp-admin-0123456789-0123456789"

LOAD = textwrap.dedent("""
    import importlib.util, json, logging, os
    from fastapi.testclient import TestClient
    logs = []
    class H(logging.Handler):
        def emit(self, rec): logs.append(rec.getMessage())
    logging.getLogger("agentic_os").addHandler(H()); logging.getLogger("agentic_os").setLevel(logging.DEBUG)
    def load_cp():
        from agentic_os import control_plane as cp
        return cp
    def load_demo():
        spec = importlib.util.spec_from_file_location("d", "deploy/permissions-showcase-demo/app.py")
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
    def hdrs(r): return {k.lower(): v for k, v in r.headers.items()}
""")


def _run(body, tmp_path, **env_extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_", "TEST_", "DEMO_"))}
    env.update(PYTHONPATH=str(ROOT), PERMISSIONS_DIR=str(tmp_path / "p"), AGENTIC_OS_HOME=str(tmp_path / "h"), **env_extra)
    code = LOAD + textwrap.dedent(body)
    p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    assert p.returncode == 0, p.stderr[-1500:]
    return json.loads(p.stdout.strip().splitlines()[-1])


STD = dict(PERMISSIONS_KEY=KEY, PERMISSIONS_ADMIN_KEY=ADMIN)

# ───────────────────────── headers ─────────────────────────

HEADER_BODY = """
    out = {}
    for name, mod in (("cp", load_cp()), ("demo", load_demo())):
        c = TestClient(mod.app)
        adm = {"x-api-key": os.environ["PERMISSIONS_ADMIN_KEY"]}
        res = {"page": c.get("/permissions"), "status_ok": c.get("/api/permissions/status", headers=adm),
               "status_401": c.get("/api/permissions/status"), "status_400": c.get("/api/permissions/status?api_key=x", headers=adm)}
        if name == "demo":
            res["list"] = c.get("/api/demo/scenarios"); res["one"] = c.get("/api/demo/scenarios/support-us-masked"); res["health"] = c.get("/healthz")
        out[name] = {k: {"status": r.status_code, "h": hdrs(r), "body": r.text if k == "page" else ""} for k, r in res.items()}
    print(json.dumps({"out": out, "logs": logs}))
"""


@pytest.fixture(scope="module")
def headers(tmp_path_factory):
    return _run(HEADER_BODY, tmp_path_factory.mktemp("hdr"), **STD)["out"]


def _csp(h):
    return {d.strip().split(" ")[0]: d.strip().split(" ")[1:] for d in h["content-security-policy"].split(";") if d.strip()}


@pytest.mark.parametrize("plane", ["cp", "demo"])
class TestPageHeaders:
    def test_the_csp_is_strict_with_a_per_response_nonce(self, headers, plane):
        r = headers[plane]["page"]
        assert r["status"] == 200
        csp = _csp(r["h"])
        assert csp["default-src"] == ["'none'"] and csp["connect-src"] == ["'self'"] and csp["base-uri"] == ["'none'"]
        assert csp["form-action"] == ["'none'"] and csp["frame-ancestors"] == ["'none'"] and csp["img-src"] == ["'self'", "data:"]
        for d in ("script-src", "style-src"):
            assert len(csp[d]) == 1 and re.fullmatch(r"'nonce-[A-Za-z0-9_\-]{16,}'", csp[d][0]), (d, csp[d])
        assert "unsafe-inline" not in r["h"]["content-security-policy"] and "unsafe-eval" not in r["h"]["content-security-policy"]
        assert "*" not in r["h"]["content-security-policy"]

    def test_the_nonce_in_the_header_is_the_nonce_of_every_script_and_style_tag(self, headers, plane):
        r = headers[plane]["page"]
        nonce = _csp(r["h"])["script-src"][0].split("-", 1)[1].strip("'")
        assert _csp(r["h"])["style-src"][0] == f"'nonce-{nonce}'"
        tags = re.findall(r"<(script|style)([^>]*)>", r["body"])
        assert tags and all(f'nonce="{nonce}"' in attrs for _, attrs in tags), tags

    def test_the_other_security_headers(self, headers, plane):
        h = headers[plane]["page"]["h"]
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "no-referrer"
        assert "no-store" in h["cache-control"]

    def test_no_inline_event_handler_and_no_style_attribute_so_the_csp_needs_no_unsafe_inline(self, headers, plane):
        class T(html.parser.HTMLParser):
            bad = []
            def handle_starttag(self, tag, attrs):
                self.bad += [(tag, k) for k, _ in attrs if k.startswith("on") or k == "style"]
        t = T(); t.bad = []
        t.feed(headers[plane]["page"]["body"])
        assert t.bad == []
        assert "javascript:" not in headers[plane]["page"]["body"].lower()

    def test_the_admin_api_carries_no_store_and_the_headers_on_every_status(self, headers, plane):
        for k in ("status_ok", "status_401", "status_400"):
            h = headers[plane][k]["h"]
            assert "no-store" in h["cache-control"], k
            assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer", k
            assert _csp(h)["default-src"] == ["'none'"] and _csp(h)["frame-ancestors"] == ["'none'"], k
        assert [headers[plane][k]["status"] for k in ("status_ok", "status_401", "status_400")] == [200, 401, 400]


def test_the_public_demo_api_and_health_carry_the_non_cache_headers(headers):
    for k in ("list", "one", "health"):
        h = headers["demo"][k]["h"]
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer", k


def test_each_page_response_gets_a_fresh_nonce(tmp_path):
    r = _run("""
        c = TestClient(load_demo().app)
        a, b = c.get("/permissions"), c.get("/permissions")
        print(json.dumps({"a": hdrs(a)["content-security-policy"], "b": hdrs(b)["content-security-policy"]}))
    """, tmp_path, **STD)
    assert r["a"] != r["b"]


# ───────────────────────── redirects, docs, 503 ─────────────────────────

def test_a_trailing_slash_redirect_never_echoes_the_host_header_or_an_http_scheme(tmp_path):
    r = _run("""
        out = {}
        for name, mod in (("cp", load_cp()), ("demo", load_demo())):
            c = TestClient(mod.app, follow_redirects=False)
            for path in ("/permissions/", "/api/permissions/status/", "/api/demo/scenarios/"):
                x = c.get(path, headers={"host": "evil.example", "x-forwarded-host": "evil.example", "x-forwarded-proto": "http"})
                out[name + " " + path] = {"status": x.status_code, "location": x.headers.get("location", ""), "body": x.text[:200]}
        print(json.dumps(out))
    """, tmp_path, **STD)
    for k, v in r.items():
        assert "evil.example" not in v["location"] + v["body"] and not v["location"].startswith(("http:", "https:", "//")), (k, v)
    assert all(v["status"] == 404 for k, v in r.items() if k.startswith("demo"))                      # the showcase: no redirect at all
    assert r["cp /permissions/"]["status"] in (307, 308) and r["cp /permissions/"]["location"] == "/permissions"


def test_docs_redoc_and_openapi_are_off_on_the_showcase_and_closed_on_the_control_plane(tmp_path):
    r = _run("""
        out = {}
        demo, cp = load_demo(), load_cp()
        for name, mod in (("demo", demo), ("cp", cp)):
            c = TestClient(mod.app)
            for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
                out[f"{name} {path}"] = c.get(path).status_code
        c = TestClient(cp.app)
        out["cp openapi authed"] = c.get("/openapi.json", headers={"x-api-key": "fleet-" + "f" * 30}).status_code
        out["cp openapi wrong"] = c.get("/openapi.json", headers={"x-api-key": "nope"}).status_code
        print(json.dumps(out))
    """, tmp_path, AGENTIC_OS_API_KEY="fleet-" + "f" * 30, **STD)
    assert all(r[f"demo {p}"] == 404 for p in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"))
    assert all(r[f"cp {p}"] in (401, 404, 503) for p in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"))
    assert r["cp openapi authed"] == 200 and r["cp openapi wrong"] == 401


def test_the_503_body_names_no_variable_and_the_fix_is_logged_server_side(tmp_path):
    r = _run("""
        out = {}
        for name, mod in (("cp", load_cp()), ("demo", load_demo())):
            x = TestClient(mod.app).get("/api/permissions/status")
            out[name] = {"status": x.status_code, "body": x.text}
        # the control plane's fleet routes too
        y = TestClient(load_cp().app).post("/dispatch", json={})
        out["fleet"] = {"status": y.status_code, "body": y.text}
        print(json.dumps({"out": out, "logs": logs}))
    """, tmp_path, PERMISSIONS_KEY=KEY)
    for k, v in r["out"].items():
        assert v["status"] == 503 and not re.search(r"[A-Z]{3,}_[A-Z_]{3,}", v["body"]), (k, v)
    assert any("PERMISSIONS_ADMIN_KEY" in m for m in r["logs"])


# ───────────────────────── throttle ─────────────────────────

THROTTLE = """
    from agentic_os import permissions_http as H
    out = {}
    cp = load_cp()
    c = TestClient(cp.app)
    good = {"x-api-key": os.environ["PERMISSIONS_ADMIN_KEY"]}
    xff = {"x-forwarded-for": "203.0.113.5"}                      # a trusted proxy fronts the service: the client identity is trustworthy
    codes = [c.get("/api/permissions/status", headers={"x-api-key": "wrong-" + str(i), **xff}).status_code for i in range(H.FAIL_LIMIT + 3)]
    out["codes"] = codes
    out["retry_after"] = c.get("/api/permissions/status", headers={"x-api-key": "x", **xff}).headers.get("retry-after")
    out["good_while_locked"] = c.get("/api/permissions/status", headers={**good, **xff}).status_code
    out["size"] = len(H._failures)
    print(json.dumps(out))
"""


def test_failed_attempts_are_throttled_per_client_then_429_with_retry_after(tmp_path):
    r = _run(THROTTLE, tmp_path, PERMISSIONS_TRUSTED_PROXIES="testclient", **STD)
    limit = r["codes"].index(429)
    assert r["codes"][:limit] == [401] * limit and limit >= 5 and set(r["codes"][limit:]) == {429}
    assert r["retry_after"] and int(r["retry_after"]) > 0
    assert r["good_while_locked"] == 429                      # a locked client is locked for good guesses too: no oracle while locked


def test_a_success_clears_the_clients_failures(tmp_path):
    r = _run("""
        from agentic_os import permissions_http as H
        c = TestClient(load_cp().app)
        xff = {"x-forwarded-for": "203.0.113.5"}
        good = {"x-api-key": os.environ["PERMISSIONS_ADMIN_KEY"], **xff}
        for i in range(H.FAIL_LIMIT - 1): c.get("/api/permissions/status", headers={"x-api-key": "bad", **xff})
        ok = c.get("/api/permissions/status", headers=good).status_code
        again = [c.get("/api/permissions/status", headers={"x-api-key": "bad", **xff}).status_code for i in range(H.FAIL_LIMIT - 1)]
        print(json.dumps({"ok": ok, "again": again}))
    """, tmp_path, PERMISSIONS_TRUSTED_PROXIES="testclient", **STD)
    assert r["ok"] == 200 and set(r["again"]) == {401}


def test_the_failure_table_is_bounded_and_expired_entries_go(monkeypatch):
    from agentic_os import permissions_http as H
    monkeypatch.setattr(H, "_failures", {})
    monkeypatch.setattr(H, "MAX_TRACKED_CLIENTS", 50)
    for i in range(500):
        H._note_failure(f"10.0.{i // 250}.{i % 250}")
    assert len(H._failures) <= 50
    monkeypatch.setattr(H, "FAIL_WINDOW_SECONDS", 0.0)
    H._note_failure("1.2.3.4")
    assert len(H._failures) <= 2


class _Req:
    def __init__(self, peer, xff=None):
        self.client = type("C", (), {"host": peer})()
        self.headers = {"x-forwarded-for": xff} if xff else {}


def test_x_forwarded_for_is_believed_only_from_a_configured_trusted_proxy(monkeypatch):
    from agentic_os import permissions_http as H
    monkeypatch.delenv("PERMISSIONS_TRUSTED_PROXIES", raising=False)
    assert H.client_id(_Req("127.0.0.1", "203.0.113.9")) == "127.0.0.1"                 # no setting: the connection peer, whatever it claims
    monkeypatch.setenv("PERMISSIONS_TRUSTED_PROXIES", "127.0.0.1, 10.0.0.0/8")
    assert H.client_id(_Req("127.0.0.1", "203.0.113.9")) == "203.0.113.9"
    assert H.client_id(_Req("127.0.0.1", "198.51.100.7, 203.0.113.9")) == "203.0.113.9"      # rightmost untrusted: the client cannot prepend a fake
    assert H.client_id(_Req("127.0.0.1", "203.0.113.9, 10.1.2.3")) == "203.0.113.9"          # trusted hops on the right are skipped
    assert H.client_id(_Req("192.0.2.50", "203.0.113.9")) == "192.0.2.50"                  # the peer is not a trusted proxy: header ignored
    assert H.client_id(_Req("127.0.0.1", "not-an-ip")) == "127.0.0.1"
    assert H.client_id(_Req("127.0.0.1")) == "127.0.0.1"


def test_two_clients_behind_a_trusted_proxy_do_not_share_a_lockout(tmp_path):
    r = _run("""
        from agentic_os import permissions_http as H
        c = TestClient(load_cp().app)
        for i in range(H.FAIL_LIMIT + 2): c.get("/api/permissions/status", headers={"x-api-key": "bad", "x-forwarded-for": "203.0.113.1"})
        other = c.get("/api/permissions/status", headers={"x-api-key": os.environ["PERMISSIONS_ADMIN_KEY"], "x-forwarded-for": "203.0.113.2"}).status_code
        same = c.get("/api/permissions/status", headers={"x-api-key": os.environ["PERMISSIONS_ADMIN_KEY"], "x-forwarded-for": "203.0.113.1"}).status_code
        print(json.dumps({"other": other, "same": same}))
    """, tmp_path, PERMISSIONS_TRUSTED_PROXIES="testclient,127.0.0.1", **STD)
    assert r == {"other": 200, "same": 429}


# ───────────────────────── PP-08: the showcase without the store key ─────────────────────────

def test_the_showcase_starts_and_serves_the_synthetic_api_with_no_store_key_and_builds_no_store(tmp_path):
    r = _run("""
        import agentic_os.permissions as P
        built = []
        orig = P.GrantStore.__init__
        def spy(self, *a, **k): built.append(1); return orig(self, *a, **k)
        P.GrantStore.__init__ = spy
        m = load_demo()
        c = TestClient(m.app)
        pub = [c.get("/permissions").status_code, c.get("/api/demo/scenarios").status_code, c.get("/api/demo/scenarios/support-us-masked").status_code,
               c.get("/healthz").status_code]
        adm = c.get("/api/permissions/status", headers={"x-api-key": "anything-at-all-DO-NOT-STORE"}).status_code
        print(json.dumps({"pub": pub, "adm": adm, "built": len(built), "dir_exists": os.path.exists(os.environ["PERMISSIONS_DIR"])}))
    """, tmp_path)                                   # NO PERMISSIONS_KEY, NO PERMISSIONS_ADMIN_KEY
    assert r["pub"] == [200, 200, 200, 200] and r["adm"] == 503 and r["built"] == 0 and r["dir_exists"] is False


def test_with_an_admin_key_but_no_store_key_the_public_api_works_and_the_admin_api_says_503_not_500(tmp_path):
    r = _run("""
        m = load_demo()
        c = TestClient(m.app, raise_server_exceptions=False)
        print(json.dumps({"pub": c.get("/api/demo/scenarios").status_code,
                          "adm": c.get("/api/permissions/grants", headers={"x-api-key": os.environ["PERMISSIONS_ADMIN_KEY"]}).status_code,
                          "wrong": c.get("/api/permissions/grants", headers={"x-api-key": "nope"}).status_code}))
    """, tmp_path, PERMISSIONS_ADMIN_KEY=ADMIN)
    assert r == {"pub": 200, "adm": 503, "wrong": 401}


def test_with_both_keys_the_admin_api_builds_the_store_lazily_after_authentication(tmp_path):
    r = _run("""
        import agentic_os.permissions as P
        built = []
        orig = P.GrantStore.__init__
        def spy(self, *a, **k): built.append(1); return orig(self, *a, **k)
        P.GrantStore.__init__ = spy
        c = TestClient(load_demo().app)
        c.get("/api/demo/scenarios"); c.get("/api/permissions/status", headers={"x-api-key": "wrong"})
        before = len(built)
        ok = c.get("/api/permissions/grants", headers={"x-api-key": os.environ["PERMISSIONS_ADMIN_KEY"]})
        print(json.dumps({"before": before, "after": len(built), "status": ok.status_code, "n": len(ok.json())}))
    """, tmp_path, **STD)
    assert r["before"] == 0 and r["after"] == 1 and r["status"] == 200 and r["n"] == 2          # seeded once on first use


# ───────────────────────── wording (PP-07) ─────────────────────────

def test_the_pages_claim_only_what_is_true():
    from agentic_os import permissions_demo as demo
    from agentic_os import permissions as P
    from agentic_os import views
    admin = views.permissions_page(subjects=["a"], roles=["admin"], resource=P.PREVIEW_RESOURCE, columns=P.PREVIEW_COLS,
                                   subject_kinds=list(P.SUBJECT_KINDS), resource_kinds=list(P.RESOURCE_KINDS), actions=list(P.ACTIONS),
                                   row_scopes=list(P.ROW_SCOPES))
    showcase = views.permissions_demo_page(demo.list_scenarios())
    overview = (ROOT / "agentic_os" / "views.py").read_text()
    for text in (admin, showcase):
        low = text.lower()
        assert "enterprise-grade" not in low and "enforces on every tool call" not in low and "complete row" not in low
    assert "not itself an enforcement point" in admin and "basic resource permissions" in admin and "trusted small-team" in admin
    assert "illustration with synthetic data" in showcase and "not a running enforcement point" in showcase
    assert "Fine-grained data access for every app" not in overview and "Basic resource permissions and governed tool access" in overview


# ───────────────────────── PP-13: the builders escape their static-config parameters too ─────────────────────────

def test_the_control_plane_builder_escapes_every_static_config_parameter_even_hostile_ones():
    from agentic_os import views
    evil = '"><script>window.__x=1</script><img src=x onerror=alert(1)>'
    page = views.permissions_page(
        subjects=[evil], roles=[evil], resource={"kind": evil, "name": evil, "maskable": [evil], "row_columns": [evil]},
        columns=[evil, "</script><script>alert(2)</script>"], subject_kinds=[evil], resource_kinds=[evil], actions=[evil], row_scopes=[evil],
        nonce="n0nce")

    class T(html.parser.HTMLParser):
        def __init__(self):
            super().__init__(); self.scripts = 0; self.imgs = 0; self.handlers = []
        def handle_starttag(self, tag, attrs):
            self.scripts += tag == "script"; self.imgs += tag == "img"
            self.handlers += [k for k, _ in attrs if k.startswith("on")]
    t = T(); t.feed(page)
    assert t.scripts == 1 and t.imgs == 0 and t.handlers == []                 # only the page's own script; nothing the config injected
    assert "</script><script>" not in page


def test_the_demo_builder_escapes_scenario_titles_and_ids():
    from agentic_os import views
    evil = '"><img src=x onerror=alert(1)>'
    page = views.permissions_demo_page([{"id": evil, "title": evil, "description": evil}], nonce="n0nce")
    assert "<img" not in page and 'nonce="n0nce"' in page


# ───────────────────────── PP-06 / PP-08: the showcase deploy files ─────────────────────────

class TestShowcaseContainerHardening:
    D = ROOT / "deploy" / "permissions-showcase-demo"

    def _compose(self):
        import yaml
        return yaml.safe_load((self.D / "permissions-demo.compose.yml").read_text())["services"]["permissions-api"]

    def test_the_container_is_unprivileged_read_only_with_no_capabilities_and_no_privilege_gain(self):
        c = self._compose()
        assert c["user"] == "10001:10001" and c["read_only"] is True and c["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in c["security_opt"] and "/tmp" in c["tmpfs"] and c["pids_limit"] and c["mem_limit"]

    def test_the_unconfined_profiles_stay_only_with_their_documented_reason(self):
        text = (self.D / "permissions-demo.compose.yml").read_text()
        c = self._compose()
        assert "seccomp=unconfined" in c["security_opt"] and "ONLY because of this host's limit" in text and "socketpair" in text

    def test_the_app_binds_one_address_not_all_interfaces_by_default(self):
        c = self._compose()
        env = dict(e.split("=", 1) for e in c["environment"])
        assert env["BIND_HOST"] == "${PERM_BIND_HOST:-127.0.0.1}"
        df = (self.D / "Dockerfile").read_text()
        assert "0.0.0.0" not in df.replace("never 0.0.0.0", "") and 'exec uvicorn app:app --host \\"$BIND_HOST\\"' in df and "BIND_HOST=127.0.0.1" in df
        assert "PERM_BIND_HOST" in (self.D / "deploy.sh").read_text()

    def test_the_image_runs_as_a_non_root_user_with_pinned_dependencies(self):
        df = (self.D / "Dockerfile").read_text()
        assert "USER app" in df and "--uid 10001" in df
        pip = re.search(r"pip install[^\n]*", df).group(0)
        specs = re.findall(r'"([^"]+)"', pip)
        assert specs and all("==" in s for s in specs), specs

    def test_the_compose_comments_are_not_stale(self):
        text = (self.D / "permissions-demo.compose.yml").read_text()
        assert "simulated per visitor" not in text and "api/demo" in text
