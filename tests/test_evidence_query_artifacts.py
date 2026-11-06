"""Artifact integrity independent of SQL read-only enforcement: the worker cannot modify ledger files, checkpoints,
manifests or their hashes; artifact roots are confined (symlink and `..` escapes fail); tampering is detected."""
from __future__ import annotations

import os
import stat

import pytest

from agentic_os.evidence_query import (
    ArtifactError, ArtifactFile, ArtifactRoot, DatasetSpec, IntegrityError, Limits, QueryRefused, ScopeCatalog,
    worker as W)
from tests.evidence_query_support import (
    ALPHA_ROWS, CANARY, EVENT_COLS, make_ceiling, make_service, owner, sha256_file, tree_state, write_jsonl)
from agentic_os.evidence_query import EvidenceQueryService, MemoryAuditSink


@pytest.fixture()
def art(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    ledger = root / "ledger.jsonl"
    write_jsonl(ledger, ALPHA_ROWS)
    (root / "sub").mkdir()
    write_jsonl(root / "sub" / "checkpoint.jsonl", ALPHA_ROWS[:1])
    (root / "manifest.json").write_text('{"hash": "' + sha256_file(ledger) + '"}')
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.jsonl").write_text(CANARY + "\n")
    return {"root": root, "ledger": ledger, "outside": outside, "tmp": tmp_path}


def _catalog(art, *, rel="ledger.jsonl", sha=None):
    cat = ScopeCatalog()
    ar = ArtifactRoot(str(art["root"]))
    cat.register_scope("alpha", [DatasetSpec("events", EVENT_COLS, artifact=ArtifactFile(ar, rel, sha or sha256_file(art["root"] / rel), fmt="jsonl"))])
    return cat


def _svc(art, cat):
    return EvidenceQueryService(cat, make_ceiling(art["tmp"] / "p", [("owner", "evidence.alpha")]), MemoryAuditSink(), limits=Limits(timeout_s=20))


def test_dataset_loads_from_artifact_and_reports_its_hash(art):
    svc = _svc(art, _catalog(art))
    r = svc.query(owner(), "alpha", "SELECT count(*) FROM events")
    assert r.rows == [[3]] and r.dataset_hashes["events"] == sha256_file(art["ledger"])


def test_tampered_artifact_is_detected_and_refused(art):
    cat = _catalog(art)
    svc = _svc(art, cat)
    with open(art["ledger"], "ab") as fh:
        fh.write(b'{"mission_id": "forged"}\n')
    with pytest.raises(QueryRefused) as e:
        svc.query(owner(), "alpha", "SELECT 1")
    assert e.value.code == "integrity"
    assert svc.audit.receipts[-1]["reason"] == "integrity_failure"


def test_verify_scope_reports_each_artifact(art):
    cat = _catalog(art)
    assert cat.verify_scope("alpha") == {"events": True}
    os.chmod(art["ledger"], 0o644)
    art["ledger"].write_bytes(b"x")
    assert cat.verify_scope("alpha") == {"events": False}


@pytest.mark.parametrize("rel", ["../outside/secret.jsonl", "/etc/hosts", "sub/../../outside/secret.jsonl", "", ".", "a\x00b", "sub/../ledger.jsonl", "./ledger.jsonl//"])
def test_dotdot_absolute_and_odd_paths_are_refused(art, rel):
    with pytest.raises(ArtifactError):
        ArtifactRoot(str(art["root"])).read_bytes(rel)


def test_symlink_file_escape_is_refused(art):
    os.symlink(art["outside"] / "secret.jsonl", art["root"] / "link.jsonl")
    with pytest.raises(ArtifactError):
        ArtifactRoot(str(art["root"])).read_bytes("link.jsonl")


def test_symlinked_directory_escape_is_refused(art):
    os.symlink(art["outside"], art["root"] / "linkdir")
    with pytest.raises(ArtifactError):
        ArtifactRoot(str(art["root"])).read_bytes("linkdir/secret.jsonl")


def test_in_root_symlink_is_also_refused(art):
    os.symlink(art["ledger"], art["root"] / "alias.jsonl")
    with pytest.raises(ArtifactError):
        ArtifactRoot(str(art["root"])).read_bytes("alias.jsonl")


def test_symlink_swapped_in_after_registration_is_refused(art):
    cat = _catalog(art)
    svc = _svc(art, cat)
    os.replace(art["ledger"], art["root"] / "moved.jsonl")
    os.symlink(art["outside"] / "secret.jsonl", art["ledger"])
    with pytest.raises(QueryRefused):
        svc.query(owner(), "alpha", "SELECT 1")


def test_root_itself_must_be_a_real_directory(art):
    with pytest.raises(ArtifactError):
        ArtifactRoot(str(art["root"] / "ledger.jsonl"))
    with pytest.raises(ArtifactError):
        ArtifactRoot(str(art["tmp"] / "does-not-exist"))


def test_oversized_artifact_is_refused(art):
    with pytest.raises(ArtifactError):
        ArtifactRoot(str(art["root"]), max_bytes=10).read_bytes("ledger.jsonl")


# ───────────────── the worker cannot modify artifacts by ANY DuckDB route ─────────────────
def _write_routes(art):
    root, led = str(art["root"]), str(art["ledger"])
    return [
        f"COPY (SELECT 'x' a) TO '{led}'",
        f"COPY events TO '{root}/new.csv' (FORMAT CSV)",
        f"COPY (SELECT 1) TO '{root}/sub/checkpoint.jsonl'",
        f"EXPORT DATABASE '{root}/export'",
        f"ATTACH '{led}' AS l",
        f"ATTACH '{root}/created.db' AS c",
        f"ATTACH '{root}/manifest.json' AS m (READ_ONLY)",
        f"SET temp_directory = '{root}/tmp'",
        f"CREATE TABLE t AS SELECT * FROM events; COPY t TO '{root}/t.parquet'",
        f"ATTACH '{root}/fb.db'; CREATE TABLE fb.t AS SELECT 1",
        f"INSTALL '{root}/ext.duckdb_extension'",
        f"LOAD '{root}/ext.duckdb_extension'",
        f"CREATE MACRO m() AS (SELECT write_log('x'))",
        f"SELECT * FROM read_text('{root}/manifest.json')",
        f"SELECT * FROM glob('{root}/*')",
        f"PRAGMA database_list",
    ]


def test_no_route_modifies_artifacts_guard_off_and_on(art):
    before = tree_state(art["root"])
    for sql in _write_routes(art):
        data = _catalog(art).load_scope("alpha")
        try:
            W.execute(data, sql, Limits(timeout_s=20), _unsafe_disable_guard=True)
        except W.WorkerError:
            pass
        assert tree_state(art["root"]) == before, sql          # bytes, inode, mtime, size, mode, listing
    svc = _svc(art, _catalog(art))
    for sql in _write_routes(art):
        with pytest.raises(QueryRefused):
            svc.query(owner(), "alpha", sql)
    assert tree_state(art["root"]) == before


def test_worker_gets_a_private_empty_scratch_and_no_artifact_path(art):
    data = _catalog(art).load_scope("alpha")
    c = W.selftest("cwd")
    assert c["listing"] == []
    assert str(art["root"]) not in repr(data.request_repr())       # the request the worker receives carries rows, not paths


def test_artifact_files_stay_read_only_for_the_loader(art):
    os.chmod(art["ledger"], 0o444)
    svc = _svc(art, _catalog(art))
    assert svc.query(owner(), "alpha", "SELECT count(*) FROM events").rows == [[3]]
    assert stat.S_IMODE(os.stat(art["ledger"]).st_mode) == 0o444
    os.chmod(art["ledger"], 0o644)


def test_loader_never_opens_artifacts_for_writing(art, monkeypatch):
    opened = []
    real = os.open
    def spy(path, flags, *a, **k):
        opened.append((str(path), flags))
        return real(path, flags, *a, **k)
    monkeypatch.setattr(os, "open", spy)
    _svc(art, _catalog(art)).query(owner(), "alpha", "SELECT 1")
    writes = [p for p, f in opened if str(art["root"]) in p and f & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)]
    assert writes == []
