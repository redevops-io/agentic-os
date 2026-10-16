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


@pytest.mark.parametrize("bad", ["/", ".", "relative/dir", "./x", ""])
def test_unusable_roots_are_ignored(cl, monkeypatch, outside_root, bad):
    monkeypatch.setenv("PROJECTS_SOURCE_ROOTS", bad)
    assert pa._source_roots() == []
    assert cl.post("/api/sources/confirm", json=_body(outside_root)).status_code == 503


def test_nul_byte_location_is_a_client_error(cl, monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECTS_SOURCE_ROOTS", str(tmp_path))
    r = cl.post("/api/sources/confirm", json=_body(str(tmp_path) + "/a\x00b"))
    assert 400 <= r.status_code < 500


def test_connector_receives_the_resolved_path(cl, monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.md").write_text("x")
    (tmp_path / "link").symlink_to(real)
    monkeypatch.setenv("PROJECTS_SOURCE_ROOTS", str(tmp_path))
    seen = []
    orig = pa.confirm_and_connect_sources
    monkeypatch.setattr(pa, "confirm_and_connect_sources",
                        lambda pid, specs, by, registry=None: seen.append([s["location"] for s in specs]) or orig(pid, specs, by))
    assert cl.post("/api/sources/confirm", json=_body(tmp_path / "link")).status_code == 200
    assert seen == [[str(real.resolve())]]


def test_symlinked_files_and_swapped_roots_are_not_read(cl, monkeypatch, tmp_path):
    from agentic_os.sources import LocalFilesConnector, SourceConnectorRegistry

    class Idx:
        def __init__(self):
            self.files = []

        def index(self, project, source, files):
            self.files.extend(files)
            return len(files)

    root, out = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    out.mkdir()
    (out / "private.txt").write_text("x")
    (root / "ok.md").write_text("y")
    (root / "link.txt").symlink_to(out / "private.txt")
    idx = Idx()
    reg = SourceConnectorRegistry()
    reg.register(LocalFilesConnector(indexer=idx))
    specs = [{"kind": "files", "location": str(root)}]
    pa.confirm_and_connect_sources("p", specs, "me", registry=reg)
    assert [os.path.basename(f) for f in idx.files] == ["ok.md"]
    # the authorised root is replaced by a link after the check
    import shutil
    shutil.rmtree(root)
    root.symlink_to(out)
    idx.files.clear()
    out_proj = pa.confirm_and_connect_sources("p", specs, "me", registry=reg)
    assert idx.files == [] and out_proj[0]["health"]["state"] != "healthy"
