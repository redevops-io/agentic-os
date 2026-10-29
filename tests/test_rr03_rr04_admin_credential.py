"""RR-03 / RR-04: grant administration takes ONLY PERMISSIONS_ADMIN_KEY, and only a key of at least 24 characters.

Before: a one-character PERMISSIONS_ADMIN_KEY worked, and the fleet-operations key (AGENTIC_OS_API_KEY) and the control token
(AGENTIC_OS_CONTROL_TOKEN) also administered grants. Now the permissions admin API is a SEPARATE privilege:

* the credential is ``PERMISSIONS_ADMIN_KEY`` only (``X-API-Key`` or ``Authorization: Bearer``); neither the fleet key nor the control token
  opens any ``/api/permissions/*`` route, on the control plane or on the showcase;
* a key shorter than 24 characters is a server misconfiguration: the API answers 503 exactly as if no key were set, and logs one clear line
  that never contains the value;
* constant-time comparison; the fleet routes (``/up``, ``/dispatch`` ...) keep their own credentials.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

from agentic_os import permissions_http

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = base64.b64encode(b"r" * 32).decode()
GOOD = "adm-" + "k" * 28                      # 32 characters
FLEET = "fleet-" + "f" * 30
TOKEN = "ctl-token-" + "t" * 30

HARNESS = textwrap.dedent("""
    import importlib.util, json, logging, os
    from fastapi.testclient import TestClient
    from agentic_os import control_plane as cp
    spec = importlib.util.spec_from_file_location("d", "deploy/permissions-showcase-demo/app.py")
    demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(demo)
    logs = []
    class H(logging.Handler):
        def emit(self, rec): logs.append(rec.getMessage())
    logging.getLogger("agentic_os").addHandler(H()); logging.getLogger("agentic_os").setLevel(logging.DEBUG)
    ROUTES = [("GET", "/api/permissions/status", None), ("GET", "/api/permissions/grants", None),
              ("POST", "/api/permissions/preview", {"subject_kind": "app", "subject": "support"}),
              ("POST", "/api/permissions/authorize", {"app": "support", "resource_name": "crm.customers"}),
              ("POST", "/api/permissions/grants", {"subject_kind": "role", "subject": "clerk", "resource_kind": "table", "resource_name": "t.x"}),
              ("PUT", "/api/permissions/grants/g-" + "0" * 32, {"row_values": ["x"]}), ("DELETE", "/api/permissions/grants/g-" + "0" * 32, None)]
    creds = json.loads(os.environ["CREDS"])
    from agentic_os import permissions_http as H
    out = {}
    for name, app in (("cp", cp.app), ("demo", demo.app)):
        c = TestClient(app, raise_server_exceptions=False)
        for cname, hdr in creds.items():
            H._failures.clear()           # the failed-attempt throttle (PP-04) is not what this module tests
            out[f"{name}/{cname}"] = sorted({c.request(m, u, json=b, headers=hdr).status_code for m, u, b in ROUTES})
    print(json.dumps({"codes": out, "logs": logs}))
""")


def _run(tmp_path, creds, **env_extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_", "TEST_", "DEMO_"))}
    env.update(PYTHONPATH=str(ROOT), PERMISSIONS_KEY=KEY, PERMISSIONS_DIR=str(tmp_path / "p"), CREDS=json.dumps(creds), **env_extra)
    p = subprocess.run([sys.executable, "-c", HARNESS], env=env, capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    assert p.returncode == 0, p.stderr[-800:]
    return json.loads(p.stdout.strip().splitlines()[-1])


CREDS = {"admin-x-api-key": {"x-api-key": GOOD}, "admin-bearer": {"authorization": "Bearer " + GOOD},
         "fleet-key": {"x-api-key": FLEET}, "fleet-key-bearer": {"authorization": "Bearer " + FLEET},
         "control-token": {"authorization": "Bearer " + TOKEN}, "control-token-as-key": {"x-api-key": TOKEN}, "none": {}}


@pytest.mark.parametrize("plane", ["cp", "demo"])
class TestOnlyThePermissionsAdminKeyAdministersGrants:
    @pytest.fixture(scope="class")
    def configured(self, tmp_path_factory):
        return _run(tmp_path_factory.mktemp("a"), CREDS, PERMISSIONS_ADMIN_KEY=GOOD, AGENTIC_OS_API_KEY=FLEET, AGENTIC_OS_CONTROL_TOKEN=TOKEN)

    @pytest.fixture(scope="class")
    def only_other_credentials(self, tmp_path_factory):
        return _run(tmp_path_factory.mktemp("b"), CREDS, AGENTIC_OS_API_KEY=FLEET, AGENTIC_OS_CONTROL_TOKEN=TOKEN)

    def test_the_permissions_admin_key_works_in_both_header_forms(self, configured, plane):
        for c in ("admin-x-api-key", "admin-bearer"):
            assert set(configured["codes"][f"{plane}/{c}"]) <= {200, 400, 404, 409, 422}, configured["codes"][f"{plane}/{c}"]

    @pytest.mark.parametrize("cred", ["fleet-key", "fleet-key-bearer", "control-token", "control-token-as-key", "none"])
    def test_the_fleet_key_and_the_control_token_administer_nothing(self, configured, plane, cred):
        assert configured["codes"][f"{plane}/{cred}"] == [401]

    @pytest.mark.parametrize("cred", ["admin-x-api-key", "fleet-key", "control-token", "none"])
    def test_with_no_permissions_admin_key_every_route_is_disabled_whatever_else_is_configured(self, only_other_credentials, plane, cred):
        assert only_other_credentials["codes"][f"{plane}/{cred}"] == [503]


@pytest.mark.parametrize("length,ok", [(1, False), (8, False), (16, False), (23, False), (24, True), (25, True), (64, True)])
def test_the_minimum_length_is_24_characters_shorter_is_a_misconfiguration_answered_503(tmp_path, length, ok):
    key = ("Z9" * 40)[:length]
    r = _run(tmp_path, {"right-key": {"x-api-key": key}, "wrong": {"x-api-key": "nope"}}, PERMISSIONS_ADMIN_KEY=key)
    for plane in ("cp", "demo"):
        if ok:
            assert 401 not in r["codes"][f"{plane}/right-key"] and 503 not in r["codes"][f"{plane}/right-key"]
            assert r["codes"][f"{plane}/wrong"] == [401]
        else:
            assert r["codes"][f"{plane}/right-key"] == [503] and r["codes"][f"{plane}/wrong"] == [503]


def test_a_short_key_logs_a_clear_line_that_never_contains_the_value(tmp_path):
    key = "short-secret-9"
    r = _run(tmp_path, {"k": {"x-api-key": key}}, PERMISSIONS_ADMIN_KEY=key)
    lines = [m for m in r["logs"] if "PERMISSIONS_ADMIN_KEY" in m]
    assert lines and all("24" in m and "disabled" in m.lower() for m in lines)
    assert not any(key in m for m in r["logs"])


def test_the_unit_policy_and_its_log(monkeypatch, caplog):
    monkeypatch.delenv("PERMISSIONS_ADMIN_KEY", raising=False)
    monkeypatch.setenv("AGENTIC_OS_API_KEY", FLEET)
    assert permissions_http.admin_key_from_env() == ""
    monkeypatch.setenv("PERMISSIONS_ADMIN_KEY", "tiny")
    with caplog.at_level("ERROR", logger="agentic_os.permissions_http"):
        assert permissions_http.admin_key_from_env() == ""
    assert "tiny" not in caplog.text and "24" in caplog.text
    monkeypatch.setenv("PERMISSIONS_ADMIN_KEY", GOOD)
    assert permissions_http.admin_key_from_env() == GOOD
    assert permissions_http.MIN_ADMIN_KEY_LENGTH == 24


def test_one_implementation_serves_both_planes_and_compares_in_constant_time():
    http_src = (ROOT / "agentic_os" / "permissions_http.py").read_text()
    assert "hmac.compare_digest" in http_src
    assert 'get("AGENTIC_OS' not in http_src and "getenv(\"AGENTIC_OS" not in http_src          # no fleet credential is read here
    cp_src = (ROOT / "agentic_os" / "control_plane.py").read_text()
    demo_src = (ROOT / "deploy" / "permissions-showcase-demo" / "app.py").read_text()
    admin_fn = cp_src.split("def require_permissions_admin")[1].split("def _build")[0]
    assert "_authorize_mutation" not in admin_fn and "_perm_require_admin(request)" in admin_fn
    assert "AGENTIC_OS_API_KEY" not in demo_src and "require_permissions_admin" in demo_src


def test_the_fleet_routes_keep_their_own_credentials(tmp_path):
    """The control token and fleet key still operate the fleet routes: only the grants API moved to its own key."""
    code = textwrap.dedent("""
        import json
        from fastapi.testclient import TestClient
        from agentic_os import control_plane as cp
        class F:
            context = type("C", (), {"resolve": staticmethod(lambda *a, **k: None)})()
            def up(self, *a): return []
            def down(self, *a): return []
        cp.fleet = F()
        c = TestClient(cp.app, raise_server_exceptions=False)
        print(json.dumps({"fleet": c.post("/up", json={}, headers={"x-api-key": "%s"}).status_code,
                          "token": c.post("/up", json={}, headers={"authorization": "Bearer %s"}).status_code,
                          "admin_key_on_fleet_route": c.post("/up", json={}, headers={"x-api-key": "%s"}).status_code}))
    """) % (FLEET, TOKEN, GOOD)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_"))}
    env.update(PYTHONPATH=str(ROOT), PERMISSIONS_KEY=KEY, PERMISSIONS_DIR=str(tmp_path / "p"), PERMISSIONS_ADMIN_KEY=GOOD,
               AGENTIC_OS_API_KEY=FLEET, AGENTIC_OS_CONTROL_TOKEN=TOKEN)
    p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    assert p.returncode == 0, p.stderr[-600:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["fleet"] not in (401, 503) and out["token"] not in (401, 503) and out["admin_key_on_fleet_route"] == 401
