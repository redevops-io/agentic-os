"""Hotfix KC-03: ``POST /api/sources/confirm`` made the server scan a CLIENT-chosen path for anyone.

An anonymous caller may only confirm folders under ``$PROJECTS_SOURCE_ROOTS``; any other ``files`` location
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


def test_anonymous_cannot_scan_arbitrary_server_path(cl):
    r = cl.post("/api/sources/confirm", json=_body("/etc"))
    assert r.status_code == 503, r.text          # fail closed: no token configured, no allow-listed root


def test_wrong_or_missing_token_is_401_when_configured(cl, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", TOKEN)
    assert cl.post("/api/sources/confirm", json=_body("/etc")).status_code == 401
    assert cl.post("/api/sources/confirm", json=_body("/etc"),
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


def test_root_escape_via_dotdot_and_symlink_is_refused(cl, monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to("/etc")
    monkeypatch.setenv("PROJECTS_SOURCE_ROOTS", str(root))
    for loc in (root / ".." / "..", root / "link", root.parent / "root-evil"):
        assert cl.post("/api/sources/confirm", json=_body(loc)).status_code == 503, loc


def test_sibling_prefix_of_root_is_not_inside_it(cl, monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data-secret").mkdir()
    monkeypatch.setenv("PROJECTS_SOURCE_ROOTS", str(tmp_path / "data"))
    assert cl.post("/api/sources/confirm", json=_body(tmp_path / "data-secret")).status_code == 503


def test_non_filesystem_kind_is_unchanged(cl):
    r = cl.post("/api/sources/confirm", json=_body("localhost/db", kind="database"))
    assert r.status_code == 200 and r.json()[0]["kind"] == "database"
