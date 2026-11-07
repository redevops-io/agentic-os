"""The About record and the build manifest record the PINNED Projects UI (D-NEW-2), verified against the real bytes.

0.6.0-rc1 recorded `ui_bundle {"id": "none", "files": [hero.jpg]}`: generate_about.py read the checkout's
agentic_os/projects_ui, which on the converged trunk holds only the tracked hero.jpg (the bundle is fetched into the WHEEL build
tree). The generator now reads the bytes that are actually packed (the agentic_os wheel in packaging/wheelhouse, or an unpacked
directory), compares them with packaging/projects_ui.lock.json and refuses the build on any difference.

Offline; synthetic roots except where the repository's own pinned artifact is read.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest

from .about_fixture import ROOT, about_record, chk_about, gen_about, make_notices, make_root, run_generate

JS = b"// pinned bundle js\n"
HTML = b"<!doctype html><title>pinned</title>\n"
CSS = b"body{}\n"
HERO = b"\xff\xd8hero"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


FILES = {"index.html": HTML, "assets/index-PINNED01.js": JS, "assets/index-PINNED01.css": CSS, "hero.jpg": HERO}


def make_artifact(tmp: Path, files=FILES) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for n, b in sorted(files.items()):
            ti = tarfile.TarInfo(n)
            ti.size = len(b)
            t.addfile(ti, io.BytesIO(b))
    return buf.getvalue()


def write_lock(root: Path, files=FILES, artifact: bytes | None = None, **over) -> dict:
    art = artifact if artifact is not None else make_artifact(root, files)
    lock = {"repo": "redevops-io/redevops-projects", "version": "local+abcdef12", "asset": "projects-ui-root.tgz",
            "sha256": sha(art), "url": "", "local_path": "packaging/ui-artifacts/projects-ui-root-abcdef12.tgz",
            "source_commit": "a" * 40, "source_commit_published": False,
            "bundle_files": {n: sha(b) for n, b in sorted(files.items())}}
    lock.update(over)
    (root / "packaging").mkdir(exist_ok=True)
    (root / "packaging" / "projects_ui.lock.json").write_text(json.dumps(lock, indent=1))
    if lock.get("local_path") and artifact is not False:
        p = root / lock["local_path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(art)
    return lock


def write_wheel(root: Path, files=FILES, marker: str | None = None, extra: dict | None = None) -> Path:
    lock = json.loads((root / "packaging" / "projects_ui.lock.json").read_text())
    wh = root / "packaging" / "wheelhouse"
    wh.mkdir(parents=True, exist_ok=True)
    w = wh / "agentic_os-0.4.1-py3-none-any.whl"
    with zipfile.ZipFile(w, "w") as z:
        z.writestr("agentic_os/__init__.py", "")
        for n, b in files.items():
            z.writestr("agentic_os/projects_ui/" + n, b)
        z.writestr("agentic_os/projects_ui/.pinned-sha256", (marker if marker is not None else lock["sha256"]) + "\n")
        for n, b in (extra or {}).items():
            z.writestr(n, b)
    return w


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("REDEVOPS_DISTRIBUTION_BUILD", raising=False)
    root = make_root(tmp_path)
    shutil.rmtree(root / "agentic_os" / "projects_ui")          # like the converged trunk: the checkout holds no bundle
    (root / "agentic_os" / "projects_ui").mkdir()
    (root / "agentic_os" / "projects_ui" / "hero.jpg").write_bytes(HERO)
    return root, make_notices(tmp_path)


def gen(root, notices, capsys, extra=()):
    rc = run_generate(root, notices, extra=extra)
    return rc, capsys.readouterr().err


def record(notices):
    return json.loads((notices / "about.json").read_bytes())


# ------------------------------------------------------------------------------------------------------ recorded
def test_record_from_the_wheel_carries_the_pin_and_the_real_file_hashes(env, capsys):
    root, notices = env
    lock = write_lock(root)
    write_wheel(root)
    rc, err = gen(root, notices, capsys)
    assert rc == 0, err
    ub = record(notices)["build"]["ui_bundle"]
    assert ub["id"] == "index-PINNED01.js"                           # no longer "none"
    assert {f["path"]: f["sha256"] for f in ub["files"]} == lock["bundle_files"]
    assert {f["path"]: f["bytes"] for f in ub["files"]}["index.html"] == len(HTML)
    assert ub["pin"] == {"version": "local+abcdef12", "artifact_sha256": lock["sha256"], "source_commit": "a" * 40,
                         "source_commit_published": False, "verified_against": "wheel"}
    assert about_record.validate_record(record(notices)) == []


def test_record_is_deterministic(env, capsys):
    root, notices = env
    write_lock(root)
    write_wheel(root)
    assert gen(root, notices, capsys)[0] == 0
    a = (notices / "about.json").read_bytes()
    assert gen(root, notices, capsys)[0] == 0
    assert (notices / "about.json").read_bytes() == a


def test_unpacked_directory_is_verified_the_same_way(env, capsys, tmp_path):
    root, notices = env
    lock = write_lock(root)
    d = tmp_path / "unpacked"
    for n, b in FILES.items():
        (d / n).parent.mkdir(parents=True, exist_ok=True)
        (d / n).write_bytes(b)
    (d / ".pinned-sha256").write_text(lock["sha256"] + "\n")
    rc, err = gen(root, notices, capsys, ["--ui-dir", str(d)])
    assert rc == 0, err
    assert record(notices)["build"]["ui_bundle"]["pin"]["verified_against"] == "directory"


def test_published_flag_is_recorded_as_given_by_the_lock(env, capsys):
    root, notices = env
    write_lock(root, source_commit_published=True)
    write_wheel(root)
    assert gen(root, notices, capsys)[0] == 0
    assert record(notices)["build"]["ui_bundle"]["pin"]["source_commit_published"] is True


def test_published_artifact_without_a_local_copy_is_verified_by_the_wheel_marker_and_file_hashes(env, capsys):
    root, notices = env
    write_lock(root, local_path="", url="https://example.invalid/projects-ui-root.tgz")
    write_wheel(root)
    rc, err = gen(root, notices, capsys)
    assert rc == 0, err


# ------------------------------------------------------------------------------------------------------ refused
def _refused(env, capsys, mutate_wheel=None, mutate_lock=None, wheel=True, files=FILES, marker=None, extra=None):
    root, notices = env
    lock = write_lock(root, **(mutate_lock or {}))
    if wheel:
        write_wheel(root, files=files, marker=marker, extra=extra)
    (notices / "about.json").write_bytes(b"{}")               # an older record must not survive a refusal
    rc, err = gen(root, notices, capsys)
    assert rc == 2, err
    assert not (notices / "about.json").exists()
    return err


def test_a_changed_bundle_file_fails_the_build_naming_the_file(env, capsys):
    err = _refused(env, capsys, files={**FILES, "assets/index-PINNED01.js": b"// tampered\n"})
    assert "assets/index-PINNED01.js" in err and "does not match" in err


def test_a_missing_bundle_file_fails_the_build(env, capsys):
    err = _refused(env, capsys, files={k: v for k, v in FILES.items() if k != "index.html"})
    assert "index.html" in err and "missing" in err


def test_a_file_the_pin_does_not_list_fails_the_build(env, capsys):
    err = _refused(env, capsys, files={**FILES, "assets/extra.js": b"x"})
    assert "assets/extra.js" in err and "not in the pinned bundle" in err


def test_a_marker_that_is_not_the_pinned_artifact_fails_the_build(env, capsys):
    err = _refused(env, capsys, marker="0" * 64)
    assert "marker" in err
    err = _refused(env, capsys, marker="local-build")
    assert "marker" in err


def test_artifact_in_the_repository_that_does_not_hash_to_the_pin_fails(env, capsys):
    root, notices = env
    lock = write_lock(root)
    write_wheel(root)
    (root / lock["local_path"]).write_bytes(b"not the pinned artifact")
    rc, err = gen(root, notices, capsys)
    assert rc == 2 and "artifact" in err and lock["sha256"] in err


def test_pinned_but_nothing_to_verify_is_not_recorded_as_none(env, capsys):
    root, notices = env
    write_lock(root)                                           # no wheel; the checkout holds only hero.jpg
    rc, err = gen(root, notices, capsys)
    assert rc == 2 and "index.html" in err
    assert not (notices / "about.json").exists()


def test_two_agentic_os_wheels_are_ambiguous_and_refused(env, capsys):
    root, notices = env
    write_lock(root)
    w = write_wheel(root)
    shutil.copy(w, w.with_name("agentic_os-0.4.2-py3-none-any.whl"))
    rc, err = gen(root, notices, capsys)
    assert rc == 2 and "more than one" in err


def test_a_lock_without_bundle_hashes_is_refused(env, capsys):
    root, notices = env
    lock = write_lock(root)
    lock.pop("bundle_files")
    (root / "packaging" / "projects_ui.lock.json").write_text(json.dumps(lock))
    write_wheel(root)
    rc, err = gen(root, notices, capsys)
    assert rc == 2 and "bundle_files" in err


def test_a_lock_without_a_source_commit_is_refused(env, capsys):
    root, notices = env
    lock = write_lock(root)
    lock.pop("source_commit")
    (root / "packaging" / "projects_ui.lock.json").write_text(json.dumps(lock))
    write_wheel(root)
    rc, err = gen(root, notices, capsys)
    assert rc == 2 and "source_commit" in err


# ------------------------------------------------------------------------------------------------------ unpinned stays as before
def test_without_a_lock_the_record_is_the_old_shape_with_no_pin(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("REDEVOPS_DISTRIBUTION_BUILD", raising=False)
    root = make_root(tmp_path)
    notices = make_notices(tmp_path)
    assert not (root / "packaging" / "projects_ui.lock.json").exists()
    rc, err = gen(root, notices, capsys)
    assert rc == 0, err
    ub = record(notices)["build"]["ui_bundle"]
    assert set(ub) == {"id", "files"} and ub["id"] == "index-TESTBUNDLE1.js"


# ------------------------------------------------------------------------------------------------------ the REAL pin and artifact
def test_the_repositorys_own_pin_matches_its_own_artifact_bytes(tmp_path):
    lock = json.loads((ROOT / "packaging" / "projects_ui.lock.json").read_text())
    art = ROOT / lock["local_path"]
    files = {}
    with tarfile.open(art, "r:gz") as t:
        for m in t.getmembers():
            if m.isfile():
                files[m.name] = t.extractfile(m).read()
    got = gen_about.verify_ui_pin(lock, {n: (len(b), sha(b)) for n, b in files.items()}, lock["sha256"], ROOT)
    assert got == []


def test_verify_ui_pin_catches_one_flipped_byte_of_the_real_bundle():
    lock = json.loads((ROOT / "packaging" / "projects_ui.lock.json").read_text())
    with tarfile.open(ROOT / lock["local_path"], "r:gz") as t:
        files = {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}
    name = next(n for n in files if n.endswith(".js"))
    files[name] = files[name][:-1] + bytes([files[name][-1] ^ 1])
    got = gen_about.verify_ui_pin(lock, {n: (len(b), sha(b)) for n, b in files.items()}, lock["sha256"], ROOT)
    assert got and name in got[0]


# ------------------------------------------------------------------------------------------------------ schema and display
def _valid(env, capsys):
    root, notices = env
    write_lock(root)
    write_wheel(root)
    assert gen(root, notices, capsys)[0] == 0
    return record(notices)


def test_validator_accepts_old_records_without_a_pin_and_rejects_a_malformed_one(env, capsys):
    doc = _valid(env, capsys)
    old = copy.deepcopy(doc)
    del old["build"]["ui_bundle"]["pin"]
    assert about_record.validate_record(old) == []
    for mutate in (lambda p: p.update(extra="x"), lambda p: p.pop("version"), lambda p: p.update(artifact_sha256="XYZ"),
                   lambda p: p.update(source_commit="abc"), lambda p: p.update(source_commit_published="no"),
                   lambda p: p.update(verified_against="somewhere"), lambda p: p.update(version="/Users/x/ui")):
        d = copy.deepcopy(doc)
        mutate(d["build"]["ui_bundle"]["pin"])
        assert about_record.validate_record(d), mutate


def test_check_about_still_accepts_the_record(env, capsys, tmp_path):
    root, notices = env
    _valid(env, capsys)
    from .about_fixture import stage_folder
    lic = stage_folder(root, notices, tmp_path / "Licenses")
    assert chk_about.check(lic, root=root, notices_report=notices / "notices-report.json") == []


def test_about_page_script_shows_the_pin():
    js = (ROOT / "agentic_os" / "about_assets" / "app.js").read_text()
    assert "ui.pin" in js and "source_commit_published" in js
    assert "Projects UI pin" in js


# ------------------------------------------------------------------------------------------------------ the build manifest
def _manifest(root, tmp_path):
    import subprocess
    import sys
    out = tmp_path / "bm.json"
    r = subprocess.run([sys.executable, "-I", str(ROOT / "packaging" / "tools" / "build_manifest.py"), "--root", str(root), "--out", str(out),
                        "--stage", "wheelhouse", "--wheelhouse", str(root / "packaging" / "wheelhouse")],
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(tmp_path)})
    return r, out


def test_build_manifest_records_the_verified_ui_pin(env, tmp_path):
    root, _ = env
    lock = write_lock(root)
    write_wheel(root)
    r, out = _manifest(root, tmp_path)
    assert r.returncode == 0, r.stderr
    ui = json.loads(out.read_text())["projects_ui"]
    assert ui["artifact_sha256"] == lock["sha256"] and ui["version"] == "local+abcdef12"
    assert ui["source_commit"] == "a" * 40 and ui["source_commit_published"] is False
    assert ui["bundle_files"] == lock["bundle_files"] and ui["verified_against"] == "wheel"
    assert ui["lock_sha256"] == hashlib.sha256((root / "packaging" / "projects_ui.lock.json").read_bytes()).hexdigest()


def test_build_manifest_fails_the_build_when_the_wheel_bytes_differ_from_the_pin(env, tmp_path):
    root, _ = env
    write_lock(root)
    write_wheel(root, files={**FILES, "index.html": b"tampered"})
    r, out = _manifest(root, tmp_path)
    assert r.returncode == 1 and "index.html" in r.stderr and "Traceback" not in r.stderr


def test_build_manifest_without_a_pin_or_a_wheel_adds_nothing(env, tmp_path):
    root, _ = env
    (root / "packaging" / "wheelhouse").mkdir(parents=True)
    r, out = _manifest(root, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "projects_ui" not in json.loads(out.read_text())
