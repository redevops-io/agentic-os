"""Pins of the Agentic Apps native build: exact, consistent, and actually used by the scripts.

Offline and fast: reads files only (plus `git` for the synthetic stale-build/lib repository).
Run with: env -u APPROVAL_SECRET python -m pytest tests/test_packaging_pins.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PK = ROOT / "packaging"
sys.path.insert(0, str(PK / "tools"))


def read_env() -> dict[str, str]:
    d = {}
    for line in (PK / "toolchain.env").read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def lock_pins(name: str) -> dict[str, str]:
    out = {}
    for line in (PK / "locks" / name).read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==(\S+)", line)
        if m:
            out[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2).rstrip("\\").strip()
    return out


def test_verify_pins_passes():
    r = subprocess.run([sys.executable, "-I", str(PK / "verify-pins.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_toolchain_versions_are_exact():
    env = read_env()
    assert env["PYTHON_VERSION"] == "3.12.10"
    assert env["PYINSTALLER_VERSION"] == "6.22.3"
    assert env["TAURI_CLI_VERSION"] == "2.11.4"
    assert env["RUST_VERSION"] == "1.98.1"
    assert env["RUNNER_IMAGE_MACOS"] == "macos-14" and env["RUNNER_IMAGE_WINDOWS"] == "windows-2022"
    for k, v in env.items():
        assert not re.search(r"latest|stable|\^|~|\*|>=", v), f"{k}={v} is not an exact pin"
    assert 'channel = "1.98.1"' in (ROOT / "deploy/launcher/rust-toolchain.toml").read_text()


def test_pyproject_git_dependencies_are_tag_pinned_not_branch_pinned():
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    built = list(proj["dependencies"]) + proj["optional-dependencies"]["projects"] + proj["optional-dependencies"]["duckdb"]
    git = [d for d in built if "git+" in d]
    assert git, "no git dependencies found"
    for d in git:
        assert re.search(r"@v\d+\.\d+\.\d+$", d), f"{d} is not pinned to a release tag"
    text = " ".join(git)
    assert "runtime-contracts.git@v0.3.4" in text          # benchmarked component: unchanged
    assert "knowledge-frontier.git@v0.1.0" in text
    assert "redevops-connectors.git@v0.1.0" in text
    assert "redevops-connectors.git@main" not in (ROOT / "pyproject.toml").read_text()


def test_git_pins_record_tag_object_and_commit():
    pins = {p["name"]: p for p in json.loads((PK / "git-pins.json").read_text())["dependencies"]}
    assert pins["redevops-connectors"]["commit"] == "7fbb9ec4d4faff1dea2e238a94495bc1951b56bc"
    assert pins["runtime-contracts"]["commit"] == "a643edc3ba5df9024532ca170afbb6e83c2256c1"
    assert pins["runtime-contracts"]["tag"] == "v0.3.4"
    assert pins["knowledge-frontier"]["commit"].startswith("d6a78ca3")
    assert pins["runtime-contracts"]["committed_build_dirs"] == ["build"]
    for p in pins.values():
        assert re.fullmatch(r"[0-9a-f]{40}", p["tag_object"]) and re.fullmatch(r"[0-9a-f]{40}", p["commit"])
        assert p["tag_object"] != p["commit"]   # annotated tags: the two differ


@pytest.mark.parametrize("os_name", ["macos", "windows", "linux"])
def test_requirements_lock_matches_what_the_frozen_build_resolved(os_name):
    pins = lock_pins(f"requirements-{os_name}.lock")
    assert pins["idna"] == "3.20" and pins["uvicorn"] == "0.53.0"      # frozen build, not the stale 3.19 / 0.52.4
    assert len(pins) == 21
    frozen = json.loads((PK / "evidence/frozen-ci-wheels.json").read_text())["platforms"][os_name]
    first_party = {"agentic-os", "runtime-contracts", "knowledge-frontier", "redevops-connectors"}
    want = {}
    for f in frozen:
        n, v = f[:-4].split("-")[:2]
        n = re.sub(r"[-_.]+", "-", n).lower()
        if n not in first_party:
            want[n] = v
    assert pins == want


@pytest.mark.parametrize("name", sorted(p.name for p in (PK / "locks").glob("*.lock")))
def test_every_lock_pin_has_hashes(name):
    cur, counts = None, {}
    for line in (PK / "locks" / name).read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==", line)
        if m:
            cur = m.group(1); counts[cur] = 0
        elif "--hash=sha256:" in line:
            assert cur
            counts[cur] += 1
    assert counts and all(c >= 1 for c in counts.values()), {k: v for k, v in counts.items() if v == 0}


def test_scripts_use_the_pins_not_floating_inputs():
    sidecar = (PK / "build-sidecar.sh").read_text()
    assert "--require-hashes" in sidecar and "pyinstaller>=" not in sidecar
    wheelhouse = (PK / "build-wheelhouse.sh").read_text()
    assert "--require-hashes" in wheelhouse and "--no-build-isolation" in (PK / "tools/build_git_wheels.py").read_text()
    launcher = (PK / "build-launcher.sh").read_text()
    assert "--locked" in launcher
    assert "build-manifest.json" in launcher and "build-manifest.json" in sidecar
    ps1 = (PK / "build-windows.ps1").read_text()
    assert "pyinstaller>=" not in ps1 and "--version ^2" not in ps1 and "@main" not in ps1


def test_workflow_is_pinned_and_adds_no_publishing_or_signing():
    wf = (ROOT / ".github/workflows/native-packaging.yml").read_text()
    live = [re.sub(r"\s+#.*$", "", l) for l in wf.splitlines() if not l.strip().startswith("#")]   # drop comments
    body = "\n".join(live)
    assert "@stable" not in body and '"^2"' not in body and not re.search(r"(ubuntu|windows|macos)-latest", body)
    for l in live:
        m = re.search(r"uses:\s*(\S+)@(\S+)", l)
        if m:
            assert re.fullmatch(r"[0-9a-f]{40}", m.group(2)), f"action not pinned by SHA: {l.strip()}"
    assert "2.11.4" in body and "1.98.1" in body and "3.12.10" in body and "macos-14" in body and "windows-2022" in body
    assert "secrets." not in body                       # nothing signs
    assert re.search(r'tags:\s*\["v\*"\]', body)         # trigger unchanged
    assert "pull_request" not in body


def test_cargo_lock_pins_frozen_ci_versions():
    lock = tomllib.loads((ROOT / "deploy/launcher/src-tauri/Cargo.lock").read_text())
    rows = {(p["name"], p["version"]) for p in lock["package"]}
    for need in [("tauri", "2.11.5"), ("tauri-build", "2.6.3"), ("serde", "1.0.229"), ("serde_json", "1.0.151"), ("tauri-utils", "2.9.3"),
                 ("wry", "0.55.1"), ("tao", "0.35.3"), ("muda", "0.19.3")]:
        assert need in rows, need
    recovered = {m.groups() for l in (PK / "cargo-targets/ci-locked-rows-recovered.txt").read_text().splitlines()
                 if (m := re.match(r"^(\S+) v(\S+)$", l.strip()))}
    assert recovered <= rows, sorted(recovered - rows)[:5]
    assert len(recovered) >= 340


# --- the stale build/lib defect (audit finding 3) ------------------------------------------------
def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(cwd)})


@pytest.fixture()
def repo_with_stale_build(tmp_path):
    r = tmp_path / "repo"
    (r / "pkg").mkdir(parents=True)
    (r / "build/lib/pkg").mkdir(parents=True)
    (r / "pkg/__init__.py").write_text("VERSION = 'new'\n")
    (r / "build/lib/pkg/__init__.py").write_text("VERSION = 'OLD'\n")      # committed stale build output
    _git(r, "init", "-q"); _git(r, "add", "-A"); _git(r, "commit", "-q", "-m", "x")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, capture_output=True, text=True).stdout.strip()
    return r, commit


def test_wheel_with_stale_build_output_is_detected(repo_with_stale_build, tmp_path):
    from build_git_wheels import verify_wheel_against_tree

    repo, commit = repo_with_stale_build
    stale = tmp_path / "stale-0.1-py3-none-any.whl"
    with zipfile.ZipFile(stale, "w") as z:
        z.writestr("pkg/__init__.py", "VERSION = 'OLD'\n")      # what a naive build packages (copied from build/lib)
        z.writestr("pkg/ghost.py", "x = 1\n")                   # a file that exists only in build output
    problems = verify_wheel_against_tree(stale, repo, commit)
    assert any("pkg/__init__.py" in p and "differs" in p for p in problems)
    assert any("ghost.py" in p and "not present" in p for p in problems)
    good = tmp_path / "good-0.1-py3-none-any.whl"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("pkg/__init__.py", "VERSION = 'new'\n")
    assert verify_wheel_against_tree(good, repo, commit) == []


def test_clean_build_dirs_removes_the_committed_build_tree(repo_with_stale_build):
    from build_git_wheels import clean_build_dirs

    repo, _ = repo_with_stale_build
    removed = clean_build_dirs(repo, ["build"])
    assert removed and not (repo / "build").exists() and (repo / "pkg/__init__.py").exists()


def test_cargo_lock_pin_semver_groups():
    from cargo_lock_pin import compat_key

    assert compat_key("1.0.229") == "1" and compat_key("0.8.2") == "0.8" and compat_key("0.0.7") == "0.0.7"
    assert compat_key("1.1.1+spec-1.1.0") == "1"


def test_build_manifest_records_tools_and_never_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_API_TOKEN", "do-not-record-this-value")
    monkeypatch.setenv("ImageVersion", "20260831.0302.1")
    out = tmp_path / "m.json"
    r = subprocess.run([sys.executable, "-I", str(PK / "tools/build_manifest.py"), "--root", str(ROOT), "--out", str(out), "--record-python",
                        "--stage", "x"], capture_output=True, text=True, env={"SOME_API_TOKEN": "do-not-record-this-value", "ImageVersion": "20260831.0302.1", "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    assert "do-not-record-this-value" not in text
    m = json.loads(text)
    assert m["runner"]["env"]["ImageVersion"] == "20260831.0302.1"
    assert m["inputs_sha256"]["packaging/toolchain.env"] and m["inputs_sha256"]["pyproject.toml"]
    assert "python_x" in m["toolchain"] and m["source"]["commit"]


def test_workflow_parses_and_runs_the_pinned_steps_in_order():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT / ".github/workflows/native-packaging.yml").read_text())
    names = [s.get("name", s.get("uses", "")) for s in wf["jobs"]["bundle"]["steps"]]
    runs = [s.get("run", "") for s in wf["jobs"]["bundle"]["steps"]]
    order = [next(i for i, r in enumerate(runs) if k in r) for k in
             ("verify-pins.py", "build-wheelhouse.sh", "build-sidecar.sh", "build-notices.sh", "stage-sidecar.sh", "tauri-cli", "build-launcher.sh", "build-sbom.sh")
             if k != "tauri-cli"]
    assert order == sorted(order), names
    assert wf["jobs"]["bundle"]["strategy"]["matrix"]["include"][2]["os"] == "macos-14"
