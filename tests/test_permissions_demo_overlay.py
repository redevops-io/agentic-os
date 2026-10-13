"""Public permissions demo: anonymous writes are simulated per session, never persisted."""
import importlib.util
import base64
import pathlib

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from agentic_os import permissions_overlay as ov  # noqa: E402

APP = pathlib.Path(__file__).resolve().parents[1] / "deploy/permissions-showcase-demo/app.py"
KEY = "a" * 64
G = {"subject_kind": "app", "subject": "newapp", "resource_kind": "table", "resource_name": "crm.customers",
     "actions": ["read"], "row_scope": "all", "masked_columns": []}


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path))
    monkeypatch.delenv("PERMISSIONS_KEY", raising=False)
    monkeypatch.delenv("PERMISSIONS_ADMIN_KEY", raising=False)
    monkeypatch.delenv("AGENTIC_OS_API_KEY", raising=False)
    spec = importlib.util.spec_from_file_location("perm_demo_app", APP)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def ids(c):
    return {g["id"] for g in c.get("/api/permissions/grants").json()}


def allowed(c):
    return c.post("/api/permissions/authorize", json={"app": "newapp", "resource_name": "crm.customers"}).json()["allowed"]


def test_unset_key_is_not_open_and_overlay_is_per_session(mod):
    a, b = TestClient(mod.app), TestClient(mod.app)
    base = ids(a)
    r = a.post("/api/permissions/grants", json=G)
    assert r.status_code == 200 and r.headers["x-demo-simulated"] == "1" and r.json()["id"]
    assert allowed(a) is True and len(ids(a)) == len(base) + 1      # same session sees it
    assert ids(b) == base and allowed(b) is False                   # other visitor does not
    assert ids(TestClient(mod.app)) == base
    assert {g.id for g in mod._GRANTS.list()} == base               # not persisted
    pv = a.post("/api/permissions/preview", json={"subject_kind": "app", "subject": "newapp"}).json()
    assert pv["limited"]["decision"]["allowed"] is True
    # simulated delete of a base grant
    gid = sorted(base)[0]
    assert a.delete(f"/api/permissions/grants/{gid}").json() == {"removed": True}
    assert gid not in ids(a) and gid in ids(b) and mod._GRANTS.get(gid)


def test_invalid_key_simulated(mod, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_ADMIN_KEY", KEY)
    c = TestClient(mod.app)
    n = len(mod._GRANTS.list())
    r = c.post("/api/permissions/grants", json=G, headers={"X-API-Key": "wrong"})
    assert r.headers.get("x-demo-simulated") == "1" and len(mod._GRANTS.list()) == n


@pytest.mark.parametrize("hdr", [{"X-API-Key": KEY},
                                 {"Authorization": "Basic " + base64.b64encode(f"admin:{KEY}".encode()).decode()}])
def test_admin_write_persists(mod, monkeypatch, hdr):
    monkeypatch.setenv("PERMISSIONS_ADMIN_KEY", KEY)
    c, other = TestClient(mod.app), TestClient(mod.app)
    r = c.post("/api/permissions/grants", json=G, headers=hdr)
    assert r.status_code == 200 and "x-demo-simulated" not in r.headers
    assert allowed(other) is True and mod._GRANTS.get(r.json()["id"])
    assert c.delete(f"/api/permissions/grants/{r.json()['id']}", headers=hdr).json() == {"removed": True}
    assert allowed(other) is False


def test_overlay_caps_and_ttl(mod):
    t = [1000.0]
    reg = ov.OverlayRegistry(mod._GRANTS, ttl=60, max_sessions=2, max_added=2, clock=lambda: t[0])
    sid, _ = reg.add(None, "app", "x1", "table", "t")
    reg.add(sid, "app", "x2", "table", "t")
    with pytest.raises(ValueError):
        reg.add(sid, "app", "x3", "table", "t")
    assert len(reg.view(sid).list()) == len(mod._GRANTS.list()) + 2
    t[0] += 61                                                     # TTL expiry drops the overlay
    assert len(reg.view(sid).list()) == len(mod._GRANTS.list()) and len(reg) == 0
    s1, _ = reg.add(None, "app", "a", "table", "t"); t[0] += 1
    s2, _ = reg.add(None, "app", "b", "table", "t"); t[0] += 1
    s3, _ = reg.add(None, "app", "c", "table", "t")
    assert len(reg) == 2 and len(reg.view(s1).list()) == len(mod._GRANTS.list())   # oldest evicted
