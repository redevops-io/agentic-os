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
    # the ONE documented deviation from the frozen wheel set (pins-16): certifi, to the fixing release
    assert want["certifi"] == "2026.7.22" and pins["certifi"] == "2026.10.19"
    want["certifi"] = pins["certifi"]
    assert pins == want


def _verify_pins():
    import importlib.util
    spec = importlib.util.spec_from_file_location("verify_pins", PK / "verify-pins.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_deviation_set_is_exactly_certifi_and_drift_elsewhere_still_fails():
    vp = _verify_pins()
    assert set(vp.DEVIATIONS) == {"certifi"}
    got = lock_pins("requirements-macos.lock")
    frozen = {vp.norm(f[:-4].split("-")[0]): f[:-4].split("-")[1]
              for f in json.loads((PK / "evidence/frozen-ci-wheels.json").read_text())["platforms"]["macos"]}
    want = {n: v for n, v in frozen.items() if n in got}
    assert vp.compare_to_frozen("macos", got, want) == []
    # another package drifting is caught
    assert vp.compare_to_frozen("macos", {**got, "idna": "3.19"}, want)
    assert vp.compare_to_frozen("macos", {**got, "uvicorn": "0.54.0"}, want)
    # the deviation drifting back to the frozen version, to another version, or below the fixing release is caught
    assert vp.compare_to_frozen("macos", {**got, "certifi": "2026.7.22"}, want)
    assert vp.compare_to_frozen("macos", {**got, "certifi": "2026.10.23"}, want)
    assert vp.compare_to_frozen("macos", {k: v for k, v in got.items() if k != "certifi"}, want)
    # the record itself: pinned >= fixing release > frozen
    d = vp.DEVIATIONS["certifi"]
    assert vp.vkey(d["pinned"]) >= vp.vkey(d["min_version"]) > vp.vkey(d["frozen"])


def test_certifi_is_pinned_to_the_same_version_in_every_lock_and_the_cutoff_is_separate():
    for name in ("requirements-macos.lock", "requirements-windows.lock", "requirements-linux.lock"):
        assert lock_pins(name)["certifi"] == "2026.10.19", name
    uni = lock_pins("../requirements.lock")
    assert uni["certifi"] == "2026.10.19"
    env = read_env()
    assert env["LOCK_EXCLUDE_NEWER"] == "2026-09-18T14:00:00Z" and env["LOCK_EXCLUDE_NEWER_CERTIFI"] > env["LOCK_EXCLUDE_NEWER"]
    assert "certifi>=2026.10.18" in (PK / "lock-constraints.in").read_text()
    regen = (PK / "regen-python-locks.sh").read_text()
    assert "--exclude-newer-package" in regen and "lock-constraints.in" in regen


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
    # the launcher crate itself is first-party: its version follows the product version (R0-prime: 0.1.1, packaging/identity.json),
    # while the recovered CI record keeps what the frozen build locked (0.1.0). Every third-party row must still be locked.
    assert ("redevops-launcher", "0.1.0") in recovered
    third_party = {r for r in recovered if r[0] != "redevops-launcher"}
    assert third_party <= rows, sorted(third_party - rows)[:5]
    assert ("redevops-launcher", "0.1.1") in rows
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


def test_compare_builds_reports_a_difference_and_its_class(tmp_path):
    """The reproducibility comparison must not say 'identical' when bytes differ (negative control)."""
    import struct

    import compare_builds as cb

    a = tmp_path / "A"; b = tmp_path / "B"
    for d in (a, b):
        d.mkdir()
    # a minimal thin Mach-O header with an LC_UUID, so the region attribution can be checked
    hdr = b"\xcf\xfa\xed\xfe" + struct.pack("<7I", 0x0100000C, 0, 2, 1, 24, 0, 0) + struct.pack("<II", 0x1B, 24) + b"\x11" * 16
    body = b"x" * 64 + b"/cargo-home/registry" + b"y" * 32
    (a / "launcher").write_bytes(hdr + body)
    (b / "launcher").write_bytes(hdr.replace(b"\x11" * 16, b"\x22" * 16) + body)
    out = tmp_path / "r.md"
    rc = cb.sys.argv = ["compare_builds.py", str(a), str(b), "--out", str(out), "--path-marker", "/cargo-home"]
    assert cb.main() == 0
    text = out.read_text()
    assert "File: DIFFERENT" in text and "'LC_UUID': 16" in text
    (b / "launcher").write_bytes(hdr + body)
    assert cb.main() == 0 and "File: IDENTICAL" in out.read_text()


# --- build order: one owner (build-all.sh); the workflow, the Windows driver and the README agree with it -----------
def _scripts_in_order(text: str) -> list[str]:
    return re.findall(r"(build-(?:wheelhouse|sidecar|notices|launcher|sbom)\.sh|stage-sidecar\.sh)", text)


def test_build_all_order_is_wheelhouse_sidecar_notices_sidecar_stage_launcher_sbom():
    seq = [s for s in _scripts_in_order("\n".join(l for l in (PK / "build-all.sh").read_text().splitlines()
                                                  if not l.lstrip().startswith("#")))]
    assert seq == ["build-wheelhouse.sh", "build-sidecar.sh", "build-notices.sh", "build-sidecar.sh",
                   "stage-sidecar.sh", "build-launcher.sh", "build-sbom.sh"]
    text = (PK / "build-all.sh").read_text()
    # pass 2 (and only pass 2) embeds the Licenses folder
    assert text.count("REDEVOPS_LICENSES_DIR=") == 1
    assert text.index("REDEVOPS_LICENSES_DIR=") > text.index("build-notices.sh")
    assert "check_notices_vs_sbom.py" in text and text.index("check_notices_vs_sbom.py") > text.index("build-sbom.sh")


def test_workflow_and_windows_driver_follow_the_same_order():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT / ".github/workflows/native-packaging.yml").read_text())
    runs = "\n".join(s.get("run", "") for s in wf["jobs"]["bundle"]["steps"])
    assert _scripts_in_order(runs) == ["build-wheelhouse.sh", "build-sidecar.sh", "build-notices.sh", "build-sidecar.sh",
                                       "stage-sidecar.sh", "build-launcher.sh", "build-sbom.sh"]
    # the pass-2 sidecar step carries REDEVOPS_LICENSES_DIR, pass 1 does not
    sidecar_steps = [s for s in wf["jobs"]["bundle"]["steps"] if "build-sidecar.sh" in s.get("run", "")]
    assert [("REDEVOPS_LICENSES_DIR" in (s.get("env") or {})) for s in sidecar_steps] == [False, True]
    assert "check_notices_vs_sbom.py" in runs
    ps1 = (PK / "build-windows.ps1").read_text()
    assert 'Run-Step "build-all.sh"' in ps1
    for forbidden in ("build-wheelhouse.sh", "build-launcher.sh", "stage-sidecar.sh"):
        assert f'Run-Step "{forbidden}"' not in ps1, "the Windows driver must not list the steps a second time"


def test_launcher_build_refuses_to_run_without_the_notices_or_the_pass_2_sidecar():
    text = (PK / "build-launcher.sh").read_text()
    assert "THIRD_PARTY_NOTICES.txt" in text and "sidecar-licenses.sha256" in text
    assert text.index("sidecar-licenses.sha256") < text.index('"${TAURI[@]}" build')
    assert "sidecar-licenses.sha256" in (PK / "build-sidecar.sh").read_text()


def test_notices_are_derived_from_the_built_sbom_not_from_a_lock():
    bn = (PK / "build-notices.sh").read_text()
    assert "--sbom" in bn and "sbom-pass1/sbom.cdx.json" in bn and "requirements.lock" not in bn
    bl = (PK / "notices" / "build-licenses.sh").read_text()
    assert "requirements.lock" not in bl
    # without --sbom the script refuses (the lock-derived stand-in needs the explicit --interim-lock)
    r = subprocess.run(["bash", str(PK / "notices" / "build-licenses.sh"), "--platform", "macos", "--site-packages", str(ROOT)],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "--sbom" in r.stderr


def test_ubuntu_acceptance_uses_no_predictable_tmp_log_and_checks_the_gate():
    text = (PK / "acceptance-ubuntu.sh").read_text()
    assert "/tmp/rdo-serve.log" not in text and "mktemp" in text
    assert "/api/projects" in text and "401" in text


def test_candidate_script_refuses_signing_variables_and_a_non_empty_output_dir(tmp_path):
    import os
    if os.uname().sysname != "Darwin" or os.uname().machine != "arm64":
        pytest.skip("macOS arm64 only")
    script = str(PK / "candidate" / "build-local-macos-candidate.sh")
    env = {**os.environ, "APPLE_SIGNING_IDENTITY": "x"}
    r = subprocess.run(["bash", script, str(tmp_path / "out")], capture_output=True, text=True, env=env)
    assert r.returncode == 2 and "signing-related variable" in r.stdout, r.stdout + r.stderr
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    env2 = {k: v for k, v in os.environ.items() if not k.startswith(("APPLE_", "CODESIGN", "TAURI_SIGNING", "KEYCHAIN"))}
    r = subprocess.run(["bash", script, str(tmp_path / "full")], capture_output=True, text=True, env=env2)
    assert r.returncode == 2 and "not empty" in r.stdout
    text = (PK / "candidate" / "build-local-macos-candidate.sh").read_text()
    for bad in ("codesign --sign", "notarytool", "security import", "gh release", "git push"):
        assert bad not in text


def test_candidate_script_uses_a_throwaway_home_a_per_build_pyinstaller_dir_and_cleans_up():
    text = (PK / "candidate" / "build-local-macos-candidate.sh").read_text()
    assert 'HOME="$W/home"' in text and 'TMPDIR="$W/tmp"' in text and "PYINSTALLER_CONFIG_DIR" in text
    assert 'HOME="$HOME"' not in text                       # the real HOME is not passed on
    assert "trap cleanup EXIT" in text and "hdiutil detach" in text
    assert "! -path './.build-work/*'" in text               # the throwaway area is not part of the evidence hashes
