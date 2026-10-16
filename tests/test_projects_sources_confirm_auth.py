"""``POST /api/sources/confirm``: file locations are restricted to configured roots.

Without a token, only folders under ``$PROJECTS_SOURCE_ROOTS`` are accepted; any other ``files`` location
needs the control token (503 when unset, 401 when wrong). Non-filesystem kinds are unchanged.
"""
import os

import pytest
from fastapi.testclient import TestClient

import agentic_os.projects_api as pa

TOKEN = "t" * 40


def _body(loc, kind="files"):
    return {"project_id": "p", "confirmed_by": "anon", "sources": [{"kind": kind, "location": str(loc)}]}


@pytest.fixture
def cl(monkeypatch):
    monkeypatch.delenv("PROJECTS_SOURCE_ROOTS", raising=False)
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)
    return TestClient(pa.create_app())


@pytest.fixture
def outside_root(tmp_path):
    d = tmp_path / "outside_root"
    d.mkdir()
    return d


def test_location_outside_configured_roots_is_refused(cl, outside_root):
    r = cl.post("/api/sources/confirm", json=_body(outside_root))
    assert r.status_code == 503, r.text          # fail closed: no token configured, no allow-listed root


def test_wrong_or_missing_token_is_401_when_configured(cl, monkeypatch, outside_root):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", TOKEN)
    assert cl.post("/api/sources/confirm", json=_body(outside_root)).status_code == 401
    assert cl.post("/api/sources/confirm", json=_body(outside_root),
                   headers={"Authorization": "Bearer nope"}).status_code == 401


def test_control_token_may_confirm_any_folder(cl, monkeypatch, tmp_path):
    (tmp_path / "a.md").write_text("x")
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", TOKEN)
    r = cl.post("/api/sources/confirm", json=_body(tmp_path), headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()[0]["stats"]["indexed"] == 1


def test_anonymous_allowed_inside_configured_root(cl, monkeypatch, tmp_path):
    (tmp_path / "a.md").write_text("x")
    monkeypatch.setenv("PROJECTS_SOURCE_ROOTS", str(tmp_path))
    r = cl.post("/api/sources/confirm", json=_body(tmp_path))
    assert r.status_code == 200 and r.json()[0]["stats"]["indexed"] == 1


def test_paths_resolving_outside_root_are_refused(cl, monkeypatch, tmp_path, outside_root):
    root = tmp_path / "allowed_root"
    root.mkdir()
    (root / "link").symlink_to(outside_root)
    monkeypatch.setenv("PROJECTS_SOURCE_ROOTS", str(root))
    for loc in (root / ".." / "..", root / "link", root.parent / "allowed_root_sibling"):
        assert cl.post("/api/sources/confirm", json=_body(loc)).status_code == 503, loc


def test_sibling_prefix_of_root_is_not_inside_it(cl, monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data-other").mkdir()
    monkeypatch.setenv("PROJECTS_SOURCE_ROOTS", str(tmp_path / "data"))
    assert cl.post("/api/sources/confirm", json=_body(tmp_path / "data-other")).status_code == 503


def test_non_filesystem_kind_is_unchanged(cl):
    r = cl.post("/api/sources/confirm", json=_body("localhost/db", kind="database"))
    assert r.status_code == 200 and r.json()[0]["kind"] == "database"
