"""scripts/fetch_projects_ui.py — pin verification, safe extraction, atomic replace (no network: file:// URLs)."""
import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "fetch_projects_ui", Path(__file__).resolve().parent.parent / "scripts" / "fetch_projects_ui.py")
fp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fp)


def _tgz(tmp_path, files, name="ui.tgz"):
    p = tmp_path / name
    with tarfile.open(p, "w:gz") as t:
        for n, data in files.items():
            ti = tarfile.TarInfo(n); ti.size = len(data); t.addfile(ti, io.BytesIO(data))
    return p


def _lock(p, sha=None):
    return {"repo": "o/r", "version": "v1", "asset": "ui.tgz",
            "sha256": sha or hashlib.sha256(p.read_bytes()).hexdigest()}


def test_unpinned_lock_changes_nothing(tmp_path):
    dest = tmp_path / "ui"; dest.mkdir(); (dest / "index.html").write_text("vendored")
    assert fp.fetch({"sha256": ""}, dest) == "unpinned"
    assert (dest / "index.html").read_text() == "vendored"


def test_verified_fetch_replaces_dest_then_is_current(tmp_path):
    arc = _tgz(tmp_path, {"index.html": b"<html>new</html>", "assets/a.js": b"1"})
    dest = tmp_path / "ui"; dest.mkdir(); (dest / "stale.txt").write_text("x")
    assert fp.fetch(_lock(arc), dest, url=arc.as_uri()) == "fetched"
    assert (dest / "index.html").read_text() == "<html>new</html>" and not (dest / "stale.txt").exists()
    assert fp.fetch(_lock(arc), dest, url=arc.as_uri()) == "current"


def test_sha_mismatch_is_rejected_and_dest_untouched(tmp_path):
    arc = _tgz(tmp_path, {"index.html": b"evil"})
    dest = tmp_path / "ui"; dest.mkdir(); (dest / "index.html").write_text("good")
    with pytest.raises(fp.FetchError, match="sha256 mismatch"):
        fp.fetch(_lock(arc, sha="0" * 64), dest, url=arc.as_uri())
    assert (dest / "index.html").read_text() == "good"


def test_path_traversal_member_is_rejected(tmp_path):
    arc = _tgz(tmp_path, {"index.html": b"x", "../escape.txt": b"y"})
    dest = tmp_path / "ui"
    with pytest.raises(fp.FetchError, match="escapes"):
        fp.fetch(_lock(arc), dest, url=arc.as_uri())
    assert not (tmp_path / "escape.txt").exists()


def test_archive_without_index_html_is_rejected(tmp_path):
    arc = _tgz(tmp_path, {"other.txt": b"x"})
    with pytest.raises(fp.FetchError, match="index.html"):
        fp.fetch(_lock(arc), tmp_path / "ui", url=arc.as_uri())


def test_local_build_marker_is_never_overwritten(tmp_path):
    arc = _tgz(tmp_path, {"index.html": b"pinned"})
    dest = tmp_path / "ui"; dest.mkdir()
    (dest / "index.html").write_text("local /projects/ build"); (dest / fp.MARKER).write_text("local-build\n")
    assert fp.fetch(_lock(arc), dest, url=arc.as_uri()) == "local"
    assert (dest / "index.html").read_text() == "local /projects/ build"
