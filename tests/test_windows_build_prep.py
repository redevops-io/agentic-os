"""Windows build preparation (R0-prime): what can be checked WITHOUT a Windows host.

No Windows artifact has been built and no Windows host was used. These tests cover the parts of the Windows pipeline that are
plain Python / bash / YAML and can therefore run on any machine:

  * git-bash behaviours simulated on POSIX (a fake `uname`, a fake native program that writes CRLF, a fake `cygpath`):
    CR in captured output, the python3 shim, the scrubbed environment of the smoke test;
  * the build manifest on an MSYS-style path and a missing artifact;
  * the first-party wheel clone under a CRLF-converting git configuration (Git for Windows default core.autocrlf=true);
  * the SBOM generator on a WINDOWS archive (PE version resources of the Microsoft DLLs, api-ms-win-* set);
  * the workflow's Windows leg: pinned, no signing, no publishing, hash-locked / --locked inputs;
  * the Windows driver build-windows.ps1 (static: it is not run; no PowerShell parser is available).

PowerShell scripts remain UNVERIFIED by a parser; the static lint of the acceptance kit is reused here.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PK = ROOT / "packaging"
sys.path.insert(0, str(PK))
sys.path.insert(0, str(PK / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

yaml = pytest.importorskip("yaml")
FROZEN = Path(os.environ["FROZEN_EVIDENCE_DIR"]) if os.environ.get("FROZEN_EVIDENCE_DIR") else Path("/nonexistent-frozen-evidence")
BASH = shutil.which("bash") or "/bin/bash"


# ---------------------------------------------------------------------------------------------------------------------
# git-bash simulation: a PATH of symlinks to the few tools common.sh needs + fake `uname` / `cygpath` / native programs
# ---------------------------------------------------------------------------------------------------------------------
NEEDED = ["bash", "env", "mkdir", "chmod", "tr", "cat", "dirname", "cut", "sed", "date", "printf", "sh", "sort", "head"]


def sim_windows(tmp_path: Path, *, python: str | None = None) -> dict:
    """A copy of packaging/{common.sh,toolchain.env} under tmp_path/packaging (so the python3 shim does not touch the repo) and a
    bin directory on PATH in which `uname -s` says MINGW64_NT-10.0, `cygpath -w X` says W:\\X and `python` (if given) is the script."""
    pk = tmp_path / "packaging"
    pk.mkdir()
    for f in ("common.sh", "toolchain.env"):
        shutil.copy(PK / f, pk / f)
    b = tmp_path / "bin"
    b.mkdir()
    for tool in NEEDED:
        real = shutil.which(tool)
        if real and not (b / tool).exists():
            (b / tool).symlink_to(real)
    (b / "uname").write_text('#!/bin/sh\n[ "$1" = "-s" ] && echo MINGW64_NT-10.0-20348 || echo MINGW64_NT\n')
    (b / "cygpath").write_text('#!/bin/sh\nprintf \'W:%s\\r\\n\' "$2"\n')   # CRLF on purpose: a native tool writes CRLF
    if python is not None:
        (b / "python").write_text(python)
    for f in b.iterdir():
        if not f.is_symlink():
            f.chmod(0o755)
    return {"pk": pk, "bin": b, "env": {"PATH": str(b), "HOME": str(tmp_path), "LANG": "C"}}


def run_bash(sim: dict, script: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(sim["env"], **(extra_env or {}))
    return subprocess.run([BASH, "-c", f'set -euo pipefail; . "{sim["pk"]}/common.sh"; {script}'], env=env, capture_output=True, text=True)


CRLF_PYTHON = "#!/bin/sh\nprintf '3.12.10\\r\\n'\n"        # what `python -c "print(platform.python_version())"` writes on Windows, even into a pipe


def test_the_simulation_reports_windows(tmp_path):
    sim = sim_windows(tmp_path, python=CRLF_PYTHON)
    r = run_bash(sim, 'echo "OS=$OS"')
    assert r.returncode == 0 and "OS=windows" in r.stdout, r.stderr


def test_assert_python_survives_the_carriage_return_of_a_native_windows_python(tmp_path):
    """Defect (STATIC, simulated): bash's $(...) strips only "\\n", so on Windows the captured version was '3.12.10\\r' and the
    very first check of build-wheelhouse.sh ('interpreter is 3.12.10, pinned CPython is 3.12.10') aborted the build."""
    sim = sim_windows(tmp_path, python=CRLF_PYTHON)
    ok = run_bash(sim, f'assert_python "{sim["bin"]}/python"; echo PASSED')
    assert ok.returncode == 0 and "PASSED" in ok.stdout, ok.stdout + ok.stderr
    (sim["bin"] / "python").write_text("#!/bin/sh\nprintf '3.12.9\\r\\n'\n")
    (sim["bin"] / "python").chmod(0o755)
    bad = run_bash(sim, f'assert_python "{sim["bin"]}/python"; echo PASSED')
    assert bad.returncode == 1 and "interpreter is 3.12.9" in bad.stdout and "PASSED" not in bad.stdout


def test_strip_cr_removes_only_carriage_returns(tmp_path):
    sim = sim_windows(tmp_path)
    r = run_bash(sim, "printf 'a\\r\\nb c\\r\\n' | strip_cr")
    assert r.returncode == 0 and r.stdout == "a\nb c\n"


def test_python3_shim_is_created_on_windows_when_only_python_exists(tmp_path):
    """build-licenses.sh calls a bare `python3`; the Microsoft Store alias or a missing python3.exe must not stop the build."""
    sim = sim_windows(tmp_path, python="#!/bin/sh\necho \"python-says:$*\"\n")
    r = run_bash(sim, 'python3 -c x')
    assert r.returncode == 0, r.stderr
    assert "python-says:-c x" in r.stdout
    assert (sim["pk"] / "dist/.pyshim/python3").is_file()


def test_python3_shim_is_not_created_when_python3_works(tmp_path):
    sim = sim_windows(tmp_path, python="#!/bin/sh\nexit 0\n")
    (sim["bin"] / "python3").write_text("#!/bin/sh\nexit 0\n")
    (sim["bin"] / "python3").chmod(0o755)
    r = run_bash(sim, "true")
    assert r.returncode == 0 and not (sim["pk"] / "dist/.pyshim").exists()


def test_scrubbed_run_on_windows_gives_the_native_program_windows_paths_and_no_host_secrets(tmp_path):
    sim = sim_windows(tmp_path)
    (sim["bin"] / "dumpenv").write_text("#!/bin/sh\nenv\n")
    (sim["bin"] / "dumpenv").chmod(0o755)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    r = run_bash(sim, f'scrubbed_run "{scratch}" dumpenv', {"GH_TOKEN_CANARY": "do-not-leak", "SYSTEMROOT": "C:\\Windows"})
    assert r.returncode == 0, r.stderr
    got = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    assert got["TEMP"] == f"W:{scratch}" and got["TMP"] == got["TEMP"] and got["USERPROFILE"] == got["TEMP"]   # CR of cygpath stripped
    assert got["SYSTEMROOT"] == "C:\\Windows" and got["TMPDIR"] == str(scratch)
    assert "GH_TOKEN_CANARY" not in got and "do-not-leak" not in r.stdout


def test_scrubbed_run_on_posix_is_unchanged(tmp_path):
    scratch = tmp_path / "s"
    scratch.mkdir()
    script = f'. "{PK}/common.sh"; scrubbed_run "{scratch}" /usr/bin/env'
    r = subprocess.run([BASH, "-c", script], env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CANARY": "x"}, capture_output=True, text=True)
    got = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    assert set(got) >= {"PATH", "HOME", "TMPDIR"} and "CANARY" not in got and "TEMP" not in got


def test_no_script_compares_or_uses_python_output_without_stripping_a_windows_carriage_return():
    """Every command substitution in the build scripts that runs a Python interpreter and prints into a variable must go through
    strip_cr (or be `echo`ed only). New occurrences fail this test until they are handled."""
    offenders = []
    for f in sorted(PK.glob("build-*.sh")) + [PK / "common.sh", PK / "stage-sidecar.sh"]:
        for n, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
            if re.search(r'\$\("?\$\{?[A-Za-z_]*(PY|py)\b[^)]*\s-c\s', code) or re.search(r'\$\(.*pyinstaller.*--version', code):
                if "strip_cr" not in code and "$(date" not in code:
                    offenders.append(f"{f.name}:{n}: {line.strip()[:110]}")
    assert offenders == [], "\n".join(offenders)


def test_pyinstaller_version_check_ignores_a_windows_carriage_return():
    text = (PK / "build-sidecar.sh").read_text()
    line = next(l for l in text.splitlines() if "pyinstaller\" --version" in l)
    assert "strip_cr" in line and line.index("strip_cr") < line.index("grep -qx")


def test_launcher_build_refuses_a_non_x64_msvc_toolchain_on_windows():
    text = (PK / "build-launcher.sh").read_text()
    assert 'x86_64-pc-windows-msvc' in text and '[ "$OS" = windows ]' in text
    assert text.index("x86_64-pc-windows-msvc") < text.index('"${TAURI[@]}" build')


# ---------------------------------------------------------------------------------------------------------------------
# build manifest
# ---------------------------------------------------------------------------------------------------------------------
def test_native_path_converts_a_git_bash_drive_path_on_windows_only():
    import build_manifest as bm

    assert bm.native_path("/d/a/agentic-os/packaging/dist/x.exe", windows=True) == "D:/a/agentic-os/packaging/dist/x.exe"
    assert bm.native_path("/c", windows=True) == "C:/"
    assert bm.native_path("/d/a/x", windows=False) == "/d/a/x"
    assert bm.native_path("C:\\a\\b", windows=True) == "C:\\a\\b" and bm.native_path("rel/x", windows=True) == "rel/x"
    assert bm.native_path("/etc/hosts", windows=True) == "/etc/hosts"        # exists as given: left alone (POSIX test host)


def test_manifest_fails_when_a_named_artifact_is_missing_and_records_per_file_hashes(tmp_path):
    out = tmp_path / "m.json"
    bundle = tmp_path / "bundle"
    (bundle / "msi").mkdir(parents=True)
    (bundle / "msi" / "a.msi").write_bytes(b"x")
    base = [sys.executable, "-I", str(PK / "tools/build_manifest.py"), "--root", str(ROOT), "--out", str(out)]
    ok = subprocess.run(base + ["--artifact", f"launcher_bundles={bundle}"], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    art = json.loads(out.read_text())["artifacts"]["launcher_bundles"]
    assert art["sha256_by_file"] == {"msi/a.msi": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"}
    bad = subprocess.run(base + ["--artifact", f"sidecar={tmp_path}/nope.exe"], capture_output=True, text=True)
    assert bad.returncode == 1 and "nope.exe" in bad.stderr
    assert json.loads(out.read_text())["artifacts"]["sidecar"]["missing"] is True


# ---------------------------------------------------------------------------------------------------------------------
# first-party wheel clones under a CRLF-converting git configuration (Git for Windows default: core.autocrlf=true)
# ---------------------------------------------------------------------------------------------------------------------
def _origin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    def g(*a):
        subprocess.run(["git", *a], cwd=repo, env=env, check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    (repo / "mod.py").write_bytes(b"X = 1\nY = 2\n")                       # committed with LF, as the sources are
    g("add", "mod.py")
    g("commit", "-q", "-m", "c")
    g("tag", "-a", "v1", "-m", "v1")
    return repo


@pytest.fixture()
def crlf_git(tmp_path, monkeypatch):
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[core]\n\tautocrlf = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return cfg


def test_control_a_plain_clone_under_autocrlf_true_rewrites_the_sources(tmp_path, crlf_git):
    """The failure mode: the same commit checked out with CRLF. verify_wheel_against_tree compares bytes with `git show`."""
    repo = _origin_repo(tmp_path)
    dest = tmp_path / "plain"
    subprocess.run(["git", "clone", "-q", "--branch", "v1", "--depth", "1", f"file://{repo}", str(dest)], check=True, capture_output=True)
    assert (dest / "mod.py").read_bytes() == b"X = 1\r\nY = 2\r\n"


def test_clone_pinned_checks_the_sources_out_byte_for_byte_whatever_autocrlf_says(tmp_path, crlf_git):
    import build_git_wheels as bgw

    repo = _origin_repo(tmp_path)
    dest = tmp_path / "pinned"
    bgw.clone_pinned(f"file://{repo}", "v1", dest)
    assert (dest / "mod.py").read_bytes() == b"X = 1\nY = 2\n"
    assert bgw.git("status", "--porcelain", cwd=dest) == ""
    head = bgw.git("rev-parse", "HEAD^{commit}", cwd=dest)
    assert subprocess.run(["git", "show", f"{head}:mod.py"], cwd=dest, capture_output=True).stdout == (dest / "mod.py").read_bytes()


def test_gitattributes_pins_lf_for_the_build_tree():
    r = subprocess.run(["git", "-C", str(ROOT), "check-attr", "eol", "text", "--",
                        "packaging/build-sidecar.sh", "packaging/locks/requirements-windows.lock", "packaging/tools/build_manifest.py",
                        "deploy/launcher/src-tauri/Cargo.lock", ".github/workflows/native-packaging.yml"],
                       capture_output=True, text=True, check=True)
    lines = r.stdout.splitlines()
    assert len(lines) == 10 and all(l.endswith(": lf") or l.endswith(": set") for l in lines), r.stdout


# ---------------------------------------------------------------------------------------------------------------------
# SBOM generator on a Windows archive
# ---------------------------------------------------------------------------------------------------------------------
def _pe(ms_hi, ms_lo, ls_hi, ls_lo) -> bytes:
    return b"MZ" + b"\x00" * 100 + b"\xbd\x04\xef\xfe" + struct.pack("<III", 0x10000, (ms_hi << 16) | ms_lo, (ls_hi << 16) | ls_lo) + b"\x00" * 8


WIN_LISTING = """Options in 'x' (PKG/CArchive):
 pyi-contents-directory _internal
Contents of 'x' (PKG/CArchive):
 struct
 pyimod01_archive
 sidecar_main
 python312.dll
 VCRUNTIME140.dll
 VCRUNTIME140_1.dll
 ucrtbase.dll
 api-ms-win-core-file-l1-1-0.dll
 api-ms-win-crt-runtime-l1-1-0.dll
 libcrypto-3.dll
 libssl-3.dll
 libffi-8.dll
 _ssl.pyd
 cryptography\\hazmat\\bindings\\_rust.pyd
 certifi\\cacert.pem
 agentic_os\\projects_ui\\assets\\index-x.js
 PYZ.pyz

Contents of 'PYZ.pyz' (PYZ):
 idna
 idna.core
 os
 agentic_os.cli
 cryptography.x509
"""
WIN_FACTS = {"python_version": "3.12.10", "openssl": [{"entry": "libcrypto-3.dll", "version": "3.0.16"},
                                                      {"entry": "cryptography/hazmat/bindings/_rust.pyd", "version": "4.0.2"}],
             "ms_dlls": {"vcruntime140.dll": "14.42.34438.0", "ucrtbase.dll": "10.0.26100.1742"},
             "ui_js": [{"name": "react", "version": "18.3.1", "licence_text": "MIT"}]}


@pytest.fixture()
def win_inputs(tmp_path):
    import test_packaging_sbom as T

    (tmp_path / "listing.txt").write_text(WIN_LISTING)
    (tmp_path / "inv.json").write_text(json.dumps(T.INVENTORY))
    (tmp_path / "facts.json").write_text(json.dumps(WIN_FACTS))
    return tmp_path, T


def test_windows_listing_generates_and_verifies_with_the_facts_file(win_inputs):
    from sbom import generate_sbom, verify_sbom

    d, T = win_inputs
    assert generate_sbom.main(T.args(d) + ["--out", str(d / "out")]) == 0
    bom = json.loads((d / "out/sbom.cdx.json").read_text())
    by = {c["name"].lower(): c for c in bom["components"]}
    assert by["vcruntime140.dll"]["version"] == "14.42.34438.0" and by["ucrtbase.dll"]["version"] == "10.0.26100.1742"
    assert "vcruntime140_1.dll" in by and "cpython" in by and by["cpython"]["version"] == "3.12.10"
    assert any("api-ms-win" in n for n in by)
    assert verify_sbom.main(["--sbom", str(d / "out/sbom.cdx.json")] + T.args(d)) == 0


def test_microsoft_dll_versions_come_from_the_archived_bytes_not_only_from_a_facts_file(win_inputs):
    """Defect (OBSERVED on this tree before the fix): with real bytes the version of an UPPER-case archive entry (VCRUNTIME140.dll,
    VCRUNTIME140_1.dll) was looked up under the lower-case key, so it was lost; the api-ms-win-* component said '0 files'."""
    from sbom import analyze
    from sbom.archive import ListingArchive

    d, T = win_inputs
    blobs = {"VCRUNTIME140.dll": _pe(14, 42, 34438, 0), "VCRUNTIME140_1.dll": _pe(14, 42, 34438, 0), "ucrtbase.dll": _pe(10, 0, 26100, 1742),
             "api-ms-win-core-file-l1-1-0.dll": _pe(10, 0, 26100, 1742), "api-ms-win-crt-runtime-l1-1-0.dll": _pe(10, 0, 26100, 1742)}

    class BytesArchive(ListingArchive):
        has_bytes = True

        def read(self, name):
            return blobs.get(name, b"")

    res = analyze.analyze(BytesArchive(d / "listing.txt"), T.INVENTORY, {"python_version": "3.12.10"}, None,
                          python_version="3.12.10", pyinstaller_version="6.22.3")
    comps = {c.key: c for c in res.comps.values()}
    assert comps["ms-dll:vcruntime140.dll"].version == "14.42.34438.0" and comps["ms-dll:vcruntime140.dll"].name == "VCRUNTIME140.dll"
    assert comps["ms-dll:vcruntime140_1.dll"].version == "14.42.34438.0"
    assert comps["ms-dll:ucrtbase.dll"].version == "10.0.26100.1742"
    api = comps["ms-apiset"]
    assert api.version == "10.0.26100.1742" and any("2 api-ms-win-*.dll files" in e for e in api.evidence)


@pytest.mark.skipif(not (FROZEN / "r0-audit/licences/windows_frozen_pkg_listing.txt").exists(), reason="frozen Windows listing not available (set FROZEN_EVIDENCE_DIR)")
def test_the_frozen_windows_listing_is_attributed_completely_with_its_facts_file():
    from sbom import analyze
    from sbom.archive import ListingArchive

    import test_packaging_sbom as T

    L = FROZEN / "r0-audit/licences"
    arc = ListingArchive(L / "windows_frozen_pkg_listing.txt", L / "windows_frozen_pyz_modules.txt")
    facts = json.loads((PK / "sbom/testdata/frozen-v0.3.2-windows-facts.json").read_text())
    assert arc.is_windows and any(n.lower() == "vcruntime140.dll" for n in arc.pkg)
    inv = dict(T.INVENTORY)
    res = analyze.analyze(arc, inv, facts, None, python_version="3.12.10", pyinstaller_version="6.22.3")
    ms = {c.name: c.version for c in res.comps.values() if c.key.startswith("ms-dll:")}
    assert ms["VCRUNTIME140.dll"] == "14.42.34438.0" and ms["ucrtbase.dll"] == "10.0.26100.1742"


# ---------------------------------------------------------------------------------------------------------------------
# the workflow's Windows leg
# ---------------------------------------------------------------------------------------------------------------------
def workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/native-packaging.yml").read_text(encoding="utf-8"))


def windows_steps() -> list[dict]:
    """Steps that run on the Windows leg: every step without an `if`, or whose `if` mentions Windows."""
    out = []
    for s in workflow()["jobs"]["bundle"]["steps"]:
        cond = str(s.get("if", ""))
        if not cond or "Windows" in cond:
            out.append(s)
    return out


def test_the_windows_leg_runs_on_the_pinned_image():
    wf = workflow()
    oses = [m["os"] for m in wf["jobs"]["bundle"]["strategy"]["matrix"]["include"]]
    assert "windows-2022" in oses and not any(o.endswith("-latest") for o in oses)
    assert wf["jobs"]["bundle"]["runs-on"] == "${{ matrix.os }}"
    assert wf["jobs"]["bundle"]["env"]["UV_PYTHON_DOWNLOADS"] == "never"


SIGNING = re.compile(r"signtool|Set-AuthenticodeSignature|AzureSignTool|trusted[-_ ]?signing|osslsigncode|certutil|\.pfx|codesign|notariz|"
                     r"WIN_CERT|APPLE_|azure/login|SignCommand|certificateThumbprint", re.I)


def test_the_windows_leg_has_no_signing_publishing_or_secret_step():
    for s in windows_steps():
        blob = json.dumps(s)
        assert not SIGNING.search(blob.replace("Sign (Windows)", "")), s.get("name")
        assert "secrets." not in blob and "github.token" not in blob.lower() and "GITHUB_TOKEN" not in blob
        assert "gh release" not in blob and "upload-release-asset" not in blob and "action-gh-release" not in blob
    # signing is only ever mentioned in comments (the file's own "Signing / notarization: staged" block)
    assert not SIGNING.search(json.dumps(workflow()))      # the parsed document: comments are not part of it
    assert workflow()["permissions"] == {"contents": "read"}


def test_the_windows_leg_installs_pinned_tools_and_builds_with_locked_inputs():
    steps = windows_steps()
    runs = "\n".join(s.get("run", "") for s in steps)
    assert "cargo install tauri-cli --version 2.11.4 --locked" in runs
    toolchain = dict(l.split("=", 1) for l in (PK / "toolchain.env").read_text().splitlines() if re.match(r"^[A-Z_]+=", l))
    assert toolchain["TAURI_CLI_VERSION"] == "2.11.4" and toolchain["RUNNER_IMAGE_WINDOWS"] == "windows-2022"
    uses = {s["uses"].split("@")[0]: s for s in steps if "uses" in s}
    assert uses["actions/setup-python"]["with"]["python-version"] == toolchain["PYTHON_VERSION"] == "3.12.10"
    assert uses["astral-sh/setup-uv"]["with"]["version"] == toolchain["UV_VERSION"]
    assert uses["dtolnay/rust-toolchain"]["with"]["toolchain"] == toolchain["RUST_VERSION"]
    for s in steps:
        if "run" in s and "./packaging/" in s["run"]:
            assert s.get("shell") == "bash", s["name"]        # the pipeline is bash (git-bash on the runner), not PowerShell
    # the scripts those steps run carry the pins
    for script in ("build-wheelhouse.sh", "build-sidecar.sh", "build-notices.sh", "build-sbom.sh"):
        assert "--require-hashes" in (PK / script).read_text(), script
    assert "-- --locked" in (PK / "build-launcher.sh").read_text()
    for lock in ("requirements-windows.lock", "build-tools-windows.lock", "build-backends-windows.lock"):
        text = (PK / "locks" / lock).read_text()
        reqs = re.findall(r"(?m)^([A-Za-z0-9_.\-]+)==", text)
        assert reqs and text.count("--hash=sha256:") >= len(reqs), lock


def test_the_windows_launcher_tests_step_cannot_discard_the_build_and_uploads_its_log():
    steps = workflow()["jobs"]["bundle"]["steps"]
    t = next(s for s in steps if "Launcher tests on Windows" in s.get("name", ""))
    assert t["if"] == "runner.os == 'Windows'" and t["continue-on-error"] is True and t["shell"] == "bash"
    assert t["working-directory"] == "deploy/launcher/src-tauri" and "RDO_SIDECAR" in t["env"]
    assert t["run"].count("cargo test --locked") == 2 and "--ignored" in t["run"] and "--test-threads=1" in t["run"]
    upload = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert "packaging/dist/launcher-tests-windows.log" in upload["with"]["path"]
    names = [s.get("name", "") for s in steps]
    assert names.index(t["name"]) > next(i for i, n in enumerate(names) if n.startswith("Task 4"))     # after the installers exist
    assert names.index(t["name"]) < names.index(upload["name"])


# ---------------------------------------------------------------------------------------------------------------------
# the Windows driver (static; PowerShell is not run and not parsed)
# ---------------------------------------------------------------------------------------------------------------------
def _lint():
    spec = importlib.util.spec_from_file_location("static_check_ps", PK / "acceptance/windows-r0prime/tools/static_check_ps.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_build_windows_driver_passes_the_static_lint_and_signs_nothing(capsys):
    ps1 = PK / "build-windows.ps1"
    raw = ps1.read_bytes()
    assert all(b < 128 for b in raw) and b"\r" not in raw
    assert _lint().main([str(ps1)]) == 0, capsys.readouterr().out
    text = raw.decode()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert not SIGNING.search(code)
    assert 'Run-Step "build-all.sh"' in text and "toolchain.env" in text and 'UV_PYTHON_DOWNLOADS = "never"' in text
    assert "x86_64-pc-windows-msvc" in text


def test_the_kit_lint_accepts_every_powershell_file_of_the_kit(capsys):
    files = sorted(str(p) for p in (PK / "acceptance/windows-r0prime").rglob("*.ps1"))
    assert files
    assert _lint().main(files) == 0, capsys.readouterr().out


# ---------------------------------------------------------------------------------------------------------------------
# the Windows acceptance kit (static): identity, contract, the dated-check runbook
# ---------------------------------------------------------------------------------------------------------------------
KIT = PK / "acceptance/windows-r0prime"


def _code_lines(path: Path) -> list[str]:
    text = re.sub(r"<#.*?#>", "", path.read_text(), flags=re.S)           # comment-based help blocks
    return [l for l in text.splitlines() if not l.lstrip().startswith("#")]


def test_the_kit_does_not_hard_code_a_product_exe_or_identifier_outside_the_identity_defaults():
    pat = re.compile(r"'agentic-(apps|r0prime)(\.exe)?'|'Agentic Apps( R0-prime)?'|io\.redevops\.agentic-apps|Software\\redevops")
    offenders = []
    for f in sorted(KIT.glob("*.ps1")):
        if f.name in ("Verify-FrozenSignatures.ps1", "Common.ps1"):      # the dated check is about the FROZEN files; Common.ps1 holds the defaults
            continue
        for n, l in enumerate(_code_lines(f), 1):
            if pat.search(l):
                offenders.append(f"{f.name}:{n}: {l.strip()[:100]}")
    assert offenders == [], "\n".join(offenders)


def test_the_kit_identity_defaults_equal_the_identity_file_and_can_be_overridden_from_it():
    text = (KIT / "Common.ps1").read_text()
    for key in ("bundle_identifier", "product_name", "main_binary_name", "sidecar_binary_name", "R0P_IDENTITY_FILE", "R0P_IDENTITY_LINE", "Import-KitIdentity"):
        assert key in text, key
    assert "..\\..\\identity.json" in text
    idf = PK / "identity.json"
    if idf.is_file():                                   # present once the rc2 identity work is merged
        r0 = json.loads(idf.read_text())["r0prime"]
        assert f"$script:ProductName = '{r0['product_name']}'" in text and f"$script:AppId = '{r0['bundle_identifier']}'" in text
        assert f"$script:LauncherExe = '{r0['main_binary_name']}.exe'" in text


def test_the_handoff_check_requires_the_eight_contract_fields_and_not_the_optional_launch_code():
    text = (KIT / "06-Api-Checks.ps1").read_text()
    assert "-and $ho.launch_code" not in text
    for field in ("'v', 'proto', 'event', 'host', 'port', 'pid', 'base_path', 'secret'",):
        assert field in text
    contract = (ROOT / "docs/LOCAL_API_SECURITY.md").read_text()
    assert "Required fields: `v`, `proto`, `event`, `host`, `port`, `pid`, `base_path`, `secret`." in contract
    common = (KIT / "Common.ps1").read_text()
    assert "X-Agentic-Session" in common and "X-Agentic-CSRF" in common and "agentic_os_session" not in "\n".join(_code_lines(KIT / "Common.ps1"))


def test_the_orphan_launcher_mode_follows_the_launcher_tree_not_port_8787():
    code = "\n".join(_code_lines(KIT / "05-Regression-Orphan.ps1"))
    assert "-Port 8787 -TempDir $prof.Temp -RootPid $lp.Id" not in code and "Get-ListenersOf" in code


def test_the_start_path_test_windows_match_the_launcher_constants():
    rs = (ROOT / "deploy/launcher/src-tauri/src/launch.rs").read_text()
    deadline = int(re.search(r"START_VERIFY_DEADLINE: Duration = Duration::from_secs\((\d+)\)", rs).group(1))
    assert deadline == 30
    ps = (KIT / "09-Launcher-Lifecycle.ps1").read_text()
    assert "$secs -ge 25 -and $secs -le 45" in ps and "30 s" in ps                       # the busy window brackets the 30 s deadline
    cold = (KIT / "10-ColdStart-Timing.ps1").read_text()
    assert "30000" in cold and "90000" in cold and "READY_TIMEOUT: Duration = Duration::from_secs(90)" in rs
    main = (ROOT / "deploy/launcher/src-tauri/src/main.rs").read_text()
    assert 'std::env::var("RDO_SIDECAR")' in main                                          # the fake-sidecar tests rely on this override
    lc = (ROOT / "deploy/launcher/src-tauri/src/lifecycle.rs").read_text()
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in lc and "CREATE_NO_WINDOW" in lc


def test_run_all_includes_the_new_steps_and_the_assisted_one_is_listed_as_manual():
    text = (KIT / "Run-All.ps1").read_text()
    for s in ("08-Lifecycle-Ports.ps1", "10-ColdStart-Timing.ps1", "11-Hardening-Checks.ps1"):
        assert s in text
    assert "09-Launcher-Lifecycle.ps1" in text and "IdentityFile" in text


def test_the_dated_check_runbook_names_the_script_and_the_frozen_hashes_that_the_script_embeds():
    import hashlib

    run = (KIT / "DATED-CHECK-RUNBOOK.md").read_text()
    script = KIT / "Verify-FrozenSignatures.ps1"
    sha = hashlib.sha256(script.read_bytes()).hexdigest()
    assert sha in run, "the runbook must state the sha256 of the script it tells the person to run"
    body = script.read_text()
    for h in ("9e5e6262f7830255333060193cc4c285fcf60a72ee70905f22792c12e12d58e5", "5931b1914e732c7b8106fc19f26e63e806fd1b0f3b876b2236cd2e24410fde23",
              "A5726E26325013EE238988B8688E07FAA77B532D"):
        assert h in body and h in run
    assert "2026-11-07T03:13:00Z" in run and "2026-11-07T03:12:51Z" in run and "2026-11-07T03:13:00" in body
    assert (KIT / "tools/stage-for-windows-host.sh").is_file() and "Collect-Results.ps1" in run


def test_venv_py_names_the_real_exe_when_the_bare_name_also_resolves(tmp_path):
    """Defect found on Windows Server 2022 (2026-11-05): under git-bash `[ -x <venv>/Scripts/python ]` is true for python.exe, so venv_py
    returned the extension-less path, which the native uv.exe rejects. Simulated here with BOTH names present (the bare name executable):
    the .exe must win; with only the bare name (POSIX venv) the bare name is returned."""
    both = tmp_path / "winvenv" / "Scripts"
    both.mkdir(parents=True)
    (both / "python.exe").write_text("x")
    (both / "python").write_text("#!/bin/sh\n")
    (both / "python").chmod(0o755)
    posix = tmp_path / "posixvenv" / "bin"
    posix.mkdir(parents=True)
    (posix / "python").write_text("#!/bin/sh\n")
    (posix / "python").chmod(0o755)
    common = PK / "common.sh"
    def run(venv):
        r = subprocess.run([BASH, "-c", f'. "{common}"; venv_py "{venv}"'], capture_output=True, text=True, check=True)
        return r.stdout.strip()
    assert run(tmp_path / "winvenv").endswith("/Scripts/python.exe")
    assert run(tmp_path / "posixvenv").endswith("/bin/python")


def test_build_tooling_never_decodes_with_the_locale_codec():
    """Defect found on Windows Server 2022 (2026-11-05): packaging/sbom/rustsbom.py ran `cargo metadata` with text=True, so the output was
    decoded with the Windows ANSI code page (cp1252) and died on a UTF-8 byte (UnicodeDecodeError 0x81); the same applies on cp1251/cp932 hosts.
    Every Path.read_text()/write_text() and every text-mode subprocess in the build tooling must name its encoding (AST check)."""
    import ast

    bad = []
    for sub in ("sbom", "tools", "notices"):
        for f in sorted((PK / sub).rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                kw = {k.arg for k in n.keywords}
                name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                if name == "read_text" and n.args:
                    continue  # importlib.metadata.Distribution.read_text(filename) / the notices module's own UTF-8 reader, not Path.read_text
                if name in ("read_text", "write_text") and "encoding" not in kw and isinstance(n.func, ast.Attribute):
                    bad.append(f"{f.relative_to(ROOT)}:{n.lineno} {name}")
                if name == "run" and "text" in kw and "encoding" not in kw:
                    bad.append(f"{f.relative_to(ROOT)}:{n.lineno} subprocess.run(text=True)")
    assert not bad, bad


def test_windows_launcher_link_is_deterministic_by_default():
    """Two Windows builds of one tree differed in 24 bytes of agentic-r0prime.exe (PE timestamp, debug-directory timestamps, PDB GUID).
    build-launcher.sh must pass /Brepro on Windows (opt-out LAUNCHER_BREPRO=0) and only there."""
    s = (PK / "build-launcher.sh").read_text(encoding="utf-8")
    i = s.index('if [ "$OS" = windows ] && [ "${LAUNCHER_BREPRO:-1}" = 1 ]')
    block = s[i : s.index("fi", i)]
    assert "link-arg=/Brepro" in block
    assert s.count("/Brepro") == 2 or "/Brepro" in block  # mentioned in the comment and the flag


def test_acceptance_kit_step_04_restores_its_sidecarexe_parameter_after_common_and_02_accepts_the_x86_nsis_uninstaller():
    """Windows Server 2022, 2026-11-05 (kit defects, not candidate defects): (1) Common.ps1 sets $script:SidecarExe (a file NAME), which is step
    04's own -SidecarExe parameter once dot-sourced, so BST-00 always failed 'not found'; (2) the NSIS uninstall.exe stub is a 32-bit PE by design
    and step 02 failed it for not being x64."""
    k = PK / "acceptance/windows-r0prime"
    s4 = (k / "04-Regression-Bootstrap.ps1").read_text(encoding="utf-8")
    assert s4.index("$SidecarPath = $SidecarExe") < s4.index(". (Join-Path $PSScriptRoot 'Common.ps1')")  # the caller's value is taken BEFORE Common.ps1 overwrites the variable
    code4 = s4[s4.index("Initialize-Step -Name '04-regression-bootstrap'"):]
    assert "$SidecarExe" not in code4.replace("$script:SidecarExe", "")
    s2 = (k / "02-Verify-Installed.ps1").read_text(encoding="utf-8")
    assert "$key -eq 'UNINSTALL' -and $arch -eq 'x86'" in s2


def test_acceptance_kit_step_04_tolerates_an_empty_stderr_file():
    """The success case has an EMPTY stderr; ReadAllBytes of it came back as $null through $(if ...) and GetString($null) threw (BST-99)."""
    s = (PK / "acceptance/windows-r0prime/04-Regression-Bootstrap.ps1").read_text(encoding="utf-8")
    i = s.index("function Test-BootstrapResult")
    assert "if ($null -eq $R.Err) { $R.Err = New-Object byte[] 0 }" in s[i : i + 600]


def test_acceptance_kit_reads_its_own_utf8_json_as_utf8():
    """Non-ASCII user name (Windows Server 2022, PowerShell 5.1, 2026-11-05): state.json / results-*.json are written as UTF-8 without BOM but were
    read with Get-Content -Raw, which decodes them with the ANSI code page, so every step after 01 looked for the install dir at a mojibake
    path (BIN-00/LCH-00/BST-00 'not found'). Every Get-Content ... -Raw | ConvertFrom-Json must say -Encoding UTF8."""
    import re

    k = PK / "acceptance/windows-r0prime"
    bad = []
    for f in sorted(k.glob("*.ps1")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"Get-Content[^|]*-Raw\s*\|\s*ConvertFrom-Json", line) and "-Encoding UTF8" not in line:
                bad.append(f"{f.name}:{n}")
    assert not bad, bad
