"""LocalFSStore + path safety: traversal, absolute/UNC/drive paths, ADS, reserved names, symlinks, versions."""
from __future__ import annotations

import os
import sys
import threading

import pytest


@pytest.fixture()
def st(mods, tmp_path):
    root = tmp_path / "root"
    (root / "acme").mkdir(parents=True)
    (root / "globex").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.docx").write_bytes(b"TOP SECRET")
    return mods.store.LocalFSStore(root), root, tmp_path


BAD_IDS = [
    "../x.docx", "a/../../x.docx", "a/./b.docx", "./a.docx", "a//b.docx", "/etc/passwd", "//server/share/x.docx",
    "\\\\server\\share\\x.docx", "C:\\Windows\\x.docx", "C:/Windows/x.docx", "C:x.docx", "c:foo.docx",
    "a\\b.docx", "a.docx:stream", "a.docx::$DATA", "a.docx:Zone.Identifier", "dir/file.docx:hidden",
    "CON", "con.docx", "NUL.txt", "dir/AUX.docx", "COM1.docx", "lpt9", "Com\u00b9.docx", "CONIN$", "clock$.docx",
    "trailing.dot.", "trailing space.docx ", " leading.docx", "dir./x.docx", "PROGRA~1/x.docx", "x~1.docx",
    ".hidden.docx", ".docedit/blobs/x", "a/.docedit/x", ".git/config", "", "   ", "a/\x00b.docx", "a/\nb.docx",
    "a\u202eb.docx", "a\u200bb.docx", "a<b>.docx", 'a"b.docx', "a|b.docx", "a?b.docx", "a*b.docx", "x" * 300 + ".docx",
    "/" + "a/" * 600, "..", ".", "a/..",
]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_unsafe_document_ids_are_rejected_on_every_os(mods, bad):
    with pytest.raises(mods.errors.InvalidPath):
        mods.store.safe_rel_path(bad)


@pytest.mark.parametrize("good", ["a.docx", "contracts/2026/nda v2.docx", "Quarterly Report (final).xlsx",
                                  "r\u00e9sum\u00e9.docx", "a-b_c.1.docx", "dir/sub dir/file.name.pptx"])
def test_ordinary_document_ids_are_accepted(mods, good):
    assert mods.store.safe_rel_path(good)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_unsafe_ids_never_reach_the_filesystem(st, mods, bad):
    store, root, tmp = st
    with pytest.raises(mods.errors.DocEditorError):
        store.head("acme", bad)
    assert not (root / "acme" / ".docedit").exists()


@pytest.mark.parametrize("tenant", ["", "..", "a/b", "A", "acme/../globex", "acme\\x", "-x", "x" * 80, "ac me", None])
def test_invalid_tenant_ids(st, mods, tenant):
    store, _, _ = st
    with pytest.raises(mods.errors.TenantMismatch):
        store.head(tenant, "a.docx")


def test_not_found(st, mods):
    store, _, _ = st
    with pytest.raises(mods.errors.NotFound):
        store.head("acme", "missing.docx")


def test_unicode_normalisation_is_canonical(mods):
    assert mods.store.safe_rel_path("e\u0301.docx") == "\u00e9.docx"


# ── versions ───────────────────────────────────────────────────────────────────────────────────────────────
def test_first_read_registers_version_1_with_sha256(st, mods):
    store, root, _ = st
    (root / "acme" / "a.docx").write_bytes(b"v1 bytes")
    info = store.head("acme", "a.docx")
    assert info.version == 1 and info.sha256 == mods.store.sha256_hex(b"v1 bytes") and info.size == 8
    data, again = store.read("acme", "a.docx")
    assert data == b"v1 bytes" and again == info


def test_new_versions_are_appended_never_in_place(st, mods):
    store, root, _ = st
    f = root / "acme" / "a.docx"
    f.write_bytes(b"one")
    h1 = store.head("acme", "a.docx")
    v2 = store.put_version("acme", "a.docx", b"two", expected_version=1, actor="bob", kind="edit")
    assert v2.version == 2 and v2.parent_version == 1 and v2.actor == "bob"
    assert f.read_bytes() == b"one"                                  # the customer's file is untouched
    assert store.read("acme", "a.docx", 1)[0] == b"one" and store.read("acme", "a.docx", 2)[0] == b"two"
    assert store.head("acme", "a.docx").version == 2
    assert [v.version for v in store.versions("acme", "a.docx")] == [1, 2] and h1.version == 1


def test_stale_expected_version_fails_closed_and_changes_nothing(st, mods):
    store, root, _ = st
    (root / "acme" / "a.docx").write_bytes(b"one")
    store.head("acme", "a.docx")
    store.put_version("acme", "a.docx", b"two", expected_version=1, actor="x", kind="edit")
    with pytest.raises(mods.errors.StaleVersion) as ei:
        store.put_version("acme", "a.docx", b"three", expected_version=1, actor="x", kind="edit")
    assert ei.value.extra["current"] == 2
    assert [v.version for v in store.versions("acme", "a.docx")] == [1, 2]


def test_external_edit_bumps_the_monotonic_counter(st, mods):
    store, root, _ = st
    f = root / "acme" / "a.docx"
    f.write_bytes(b"one")
    store.head("acme", "a.docx")
    store.put_version("acme", "a.docx", b"two", expected_version=1, actor="x", kind="edit")
    f.write_bytes(b"owner changed it")                                 # the owner edits the original later
    head = store.head("acme", "a.docx")
    assert head.version == 3 and head.kind == "external_edit"          # counter never reuses a number
    with pytest.raises(mods.errors.StaleVersion):
        store.put_version("acme", "a.docx", b"x", expected_version=2, actor="x", kind="edit")


def test_blobs_are_content_addressed_and_tamper_evident(st, mods):
    store, root, _ = st
    (root / "acme" / "a.docx").write_bytes(b"one")
    info = store.head("acme", "a.docx")
    blob = root / "acme" / ".docedit" / "blobs" / info.sha256
    assert blob.read_bytes() == b"one"
    blob.write_bytes(b"tampered")
    with pytest.raises(mods.errors.Conflict, match="hash check"):
        store.read("acme", "a.docx", 1)


def test_concurrent_writers_only_one_wins_per_expected_version(st, mods):
    store, root, _ = st
    (root / "acme" / "a.docx").write_bytes(b"base")
    store.head("acme", "a.docx")
    results = []

    def w(i):
        try:
            store.put_version("acme", "a.docx", f"edit{i}".encode(), expected_version=1, actor=str(i), kind="edit")
            results.append("ok")
        except mods.errors.StaleVersion:
            results.append("stale")
    ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 1 and results.count("stale") == 7
    assert store.head("acme", "a.docx").version == 2


def test_tenant_isolation_in_the_store(st, mods):
    store, root, _ = st
    (root / "acme" / "a.docx").write_bytes(b"acme secret")
    store.head("acme", "a.docx")
    with pytest.raises(mods.errors.NotFound):
        store.read("globex", "a.docx")
    assert not (root / "globex" / ".docedit").exists() or not list((root / "globex" / ".docedit").glob("blobs/*"))


def test_derived_artifacts_live_only_in_the_sidecar(st, mods):
    store, root, _ = st
    info = store.create_derived("acme", "converted/a.docx.v1.pdf", b"%PDF-", actor="c", kind="convert",
                                classification="INTERNAL")
    assert info.version == 1 and store.get_classification("acme", "converted/a.docx.v1.pdf") == "INTERNAL"
    assert not (root / "acme" / "converted").exists()


def test_classification_label_roundtrip(st, mods):
    store, root, _ = st
    (root / "acme" / "a.docx").write_bytes(b"x")
    assert store.get_classification("acme", "a.docx") is None
    store.set_classification("acme", "a.docx", "RESTRICTED")
    assert store.get_classification("acme", "a.docx") == "RESTRICTED"


# ── symlinks / junctions ───────────────────────────────────────────────────────────────────────────────────────
def _symlink(src, dst, is_dir=False):
    try:
        os.symlink(src, dst, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted on this runner")


def test_symlinked_file_escape_is_refused(st, mods):
    store, root, tmp = st
    _symlink(tmp / "outside" / "secret.docx", root / "acme" / "link.docx")
    with pytest.raises(mods.errors.InvalidPath):
        store.read("acme", "link.docx")


def test_symlinked_directory_escape_is_refused(st, mods):
    store, root, tmp = st
    _symlink(tmp / "outside", root / "acme" / "escape", is_dir=True)
    with pytest.raises(mods.errors.InvalidPath):
        store.read("acme", "escape/secret.docx")


def test_symlinked_tenant_dir_is_refused(st, mods):
    store, root, tmp = st
    _symlink(tmp / "outside", root / "evil", is_dir=True)
    with pytest.raises(mods.errors.InvalidPath):
        store.read("evil", "secret.docx")


def test_in_root_symlink_to_other_tenant_is_refused(st, mods):
    store, root, _ = st
    (root / "globex" / "g.docx").write_bytes(b"globex")
    _symlink(root / "globex" / "g.docx", root / "acme" / "steal.docx")
    with pytest.raises(mods.errors.InvalidPath):
        store.read("acme", "steal.docx")


def test_sidecar_symlink_is_refused(st, mods):
    store, root, tmp = st
    (root / "acme" / "a.docx").write_bytes(b"x")
    _symlink(tmp / "outside", root / "acme" / ".docedit", is_dir=True)
    with pytest.raises(mods.errors.InvalidPath):
        store.head("acme", "a.docx")


# ── locks / atomic writes (portable) ───────────────────────────────────────────────────────────────────────────
def test_file_lock_is_exclusive_and_released(mods, tmp_path):
    L = mods.locking
    a = L.FileLock(tmp_path / "x.lock", timeout_s=0.2)
    a.acquire()
    with pytest.raises(mods.errors.Conflict):
        L.FileLock(tmp_path / "x.lock", timeout_s=0.2).acquire()
    a.release()
    with L.FileLock(tmp_path / "x.lock", timeout_s=0.2):
        pass
    assert not (tmp_path / "x.lock").exists()


def test_stale_lock_is_broken(mods, tmp_path):
    p = tmp_path / "y.lock"
    p.write_text("dead owner")
    old = p.stat().st_mtime - 3600
    os.utime(p, (old, old))
    with mods.locking.FileLock(p, timeout_s=1, stale_s=60):
        pass


def test_atomic_write_replaces_without_partial_state(mods, tmp_path):
    p = tmp_path / "d" / "f.bin"
    mods.locking.atomic_write_bytes(p, b"one")
    mods.locking.atomic_write_bytes(p, b"two")
    assert p.read_bytes() == b"two" and [x.name for x in p.parent.iterdir()] == ["f.bin"]
