"""SC-10: a malformed request is a clean denial or a 400 — never an unhandled 500, and the plane never raises.

Covers `PermissionsPlane.authorize` (non-string resource/action), a stored grant with a malformed resource name, and a fuzz of the
control-plane and showcase request handlers with malformed JSON types."""
from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

from agentic_os.permissions import Grant, GrantStore, Identity, PermissionsPlane

KEY = base64.b64encode(b"f" * 32).decode()
ROOT = pathlib.Path(__file__).resolve().parents[1]
WEIRD = [None, 0, 7, 3.5, True, "", "  ", ["x"], [], {"a": 1}, {}, b"x".decode(), "t" * 100000]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    (tmp_path / "p").mkdir()
    return GrantStore(str(tmp_path / "p"))


class TestThePlaneNeverRaises:
    @pytest.mark.parametrize("v", WEIRD, ids=[f"w{i}" for i in range(len(WEIRD))])
    def test_a_non_string_resource_name_kind_or_action_is_a_clean_denial(self, store, v):
        store.add("role", "clerk", "table", "crm.customers")
        plane = PermissionsPlane(store)
        ident = Identity(roles=["clerk"])
        for kwargs in ({"resource_name": v}, {"resource_kind": v}, {"action": v}):
            args = {"resource_kind": "table", "resource_name": "crm.customers", "action": "read", **kwargs}
            d = plane.authorize(ident, args["resource_kind"], args["resource_name"], args["action"])
            if v not in ("crm.customers", "table", "read"):
                assert d.allowed is False

    def test_a_list_resource_name_does_not_raise(self, store):
        store.add("role", "clerk", "table", "crm.customers")
        d = PermissionsPlane(store).authorize(Identity(roles=["clerk"]), "table", ["x"], "read")
        assert d.allowed is False and "malformed request" in d.reason

    def test_a_stored_grant_with_a_non_string_resource_name_is_unusable_and_does_not_break_others(self, store):
        good = store.add("role", "clerk", "table", "crm.customers")
        raw = {"id": "g-bad", "subject_kind": "role", "subject": "clerk", "resource_kind": "table", "resource_name": ["x"],
               "actions": ["read"], "row_scope": "all", "row_column": "owner", "row_values": [], "masked_columns": [],
               "created_by": "o", "at": 1.0}
        doc = json.loads(store._box.unseal(store.path.read_bytes()))
        doc["grants"].append(raw)
        store.path.write_bytes(store._box.seal(json.dumps(doc).encode()))
        plane = PermissionsPlane(store)
        assert plane.authorize(Identity(roles=["clerk"]), "table", "crm.customers").allowed is True     # the good grant still works
        assert store.status()["malformed"] == 1 and [g.id for g in store.list()] == [good.id]

    def test_a_grant_object_with_a_malformed_resource_name_is_ignored_by_the_plane(self):
        class Facade:
            sealed = False

            def list(self):
                return [Grant(id="x", subject_kind="role", subject="r", resource_kind="table", resource_name=["x"])]
        assert PermissionsPlane(Facade()).authorize(Identity(roles=["r"]), "table", "anything").allowed is False

    def test_covers_does_not_raise_on_non_strings(self):
        g = Grant(id="x", subject_kind="role", subject="r", resource_kind="table", resource_name="crm")
        assert g.covers(["x"]) is False and g.covers(None) is False and g.covers("crm.customers") is True
        assert Grant(id="y", subject_kind="role", subject="r", resource_kind="table", resource_name=["crm"]).covers("crm") is False


FUZZ = textwrap.dedent("""
    import importlib.util, json, itertools
    from fastapi.testclient import TestClient
    from agentic_os import control_plane as cp
    spec = importlib.util.spec_from_file_location("d", "deploy/permissions-showcase-demo/app.py")
    demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(demo)
    WEIRD = [None, 0, 7, 3.5, True, "", "  ", ["x"], [], {"a": 1}, {}, "x" * 3000, [[]], {"a": [1, {"b": None}]}]
    RAW = [b"", b"not json", b"[1,2]", b"null", b"7", b'"str"', b"{", b"\\xff\\xfe", b"[" * 5000, b'{"a":' * 500]
    admin = {"X-API-Key": "adm-k-0123456789-DO-NOT-STORE"}
    worst, bad = 0, []
    def hit(client, method, url, **kw):
        global worst
        r = client.request(method, url, **kw)
        worst = max(worst, r.status_code)
        if r.status_code >= 500:
            bad.append((method, url, str(kw)[:80], r.status_code))
        return r
    for name, app in (("cp", cp.app), ("demo", demo.app)):
        c = TestClient(app, raise_server_exceptions=False)
        for raw in RAW:
            for url in ("/api/permissions/authorize", "/api/permissions/preview"):
                hit(c, "POST", url, content=raw, headers={"content-type": "application/json", **admin})
            hit(c, "POST", "/api/permissions/grants", content=raw, headers={"content-type": "application/json", **admin})
            hit(c, "PUT", "/api/permissions/grants/g-1", content=raw, headers={"content-type": "application/json", **admin})
        for field in ("app", "user", "roles", "resource_kind", "resource_name", "action", "subject_kind", "subject"):
            for v in WEIRD:
                hit(c, "POST", "/api/permissions/authorize", json={field: v}, headers=admin)
                hit(c, "POST", "/api/permissions/preview", json={field: v}, headers=admin)
        for field in ("subject_kind", "subject", "resource_kind", "resource_name", "actions", "row_scope", "row_column", "row_values",
                      "masked_columns"):
            for v in WEIRD:
                base = {"subject_kind": "role", "subject": "clerk", "resource_kind": "table", "resource_name": "t"}
                hit(c, "POST", "/api/permissions/grants", json={**base, field: v}, headers=admin)
                hit(c, "PUT", "/api/permissions/grants/g-1", json={field: v}, headers=admin)
        hit(c, "DELETE", "/api/permissions/grants/%00", headers=admin)
        hit(c, "DELETE", "/api/permissions/grants/" + "x" * 5000, headers=admin)
    print(json.dumps({"worst": worst, "bad": bad}))
""")


def test_no_request_handler_returns_a_5xx_for_malformed_json(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_"))}
    env.update(PYTHONPATH=str(ROOT), PERMISSIONS_KEY=KEY, PERMISSIONS_DIR=str(tmp_path / "p"), PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE")
    p = subprocess.run([sys.executable, "-c", FUZZ], env=env, capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    assert p.returncode == 0, p.stderr[-800:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["bad"] == [] and out["worst"] < 500


def test_the_documented_codes_for_the_common_malformed_bodies(tmp_path):
    code = textwrap.dedent("""
        import importlib.util, json
        from fastapi.testclient import TestClient
        from agentic_os import control_plane as cp
        c = TestClient(cp.app, raise_server_exceptions=False)
        h = {"content-type": "application/json", "X-API-Key": "adm-k-0123456789-DO-NOT-STORE"}
        out = {"not_json": c.post("/api/permissions/authorize", content=b"nope", headers=h).status_code,
               "list": c.post("/api/permissions/authorize", content=b"[1]", headers=h).status_code,
               "huge": c.post("/api/permissions/authorize", content=b"{" + b" " * 70000 + b"}", headers=h).status_code,
               "list_resource": c.post("/api/permissions/authorize", json={"resource_name": ["x"]}, headers=h).json()["allowed"],
               "int_user": c.post("/api/permissions/authorize", json={"user": 5}, headers=h).json()["allowed"]}
        print(json.dumps(out))
    """)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_"))}
    env.update(PYTHONPATH=str(ROOT), PERMISSIONS_KEY=KEY, PERMISSIONS_DIR=str(tmp_path / "p"), PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE")
    p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=180, cwd=str(ROOT))
    assert p.returncode == 0, p.stderr[-500:]
    assert json.loads(p.stdout.strip().splitlines()[-1]) == {"not_json": 400, "list": 400, "huge": 413, "list_resource": False,
                                                              "int_user": False}
