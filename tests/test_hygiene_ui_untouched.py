"""Regression guard for test hygiene: reading the bundled UI must not rewrite it (see tests/conftest.py hook)."""
from __future__ import annotations

from tests.conftest import _UI_DIR, _tree_digest


def test_serving_the_ui_does_not_modify_it():
    from fastapi.testclient import TestClient

    from agentic_os.projects_api import SampleProjectionProvider, create_app
    before = _tree_digest(_UI_DIR)
    c = TestClient(create_app(SampleProjectionProvider()))
    assert c.get("/").status_code == 200
    assert _tree_digest(_UI_DIR) == before


def test_digest_detects_a_change(tmp_path):
    (tmp_path / "a.js").write_text("1")
    d1 = _tree_digest(tmp_path)
    (tmp_path / "a.js").write_text("2")
    assert _tree_digest(tmp_path) != d1
