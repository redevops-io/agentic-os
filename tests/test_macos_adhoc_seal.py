"""The private unsigned macOS candidate is CONSISTENTLY ad-hoc sealed (rel/macos defect: Gatekeeper said "is damaged").

Without a seal, the bundle carried only the linker signature of the launcher: `codesign --verify --deep --strict` failed ("code has no
resources but signature indicates they must be present") and a quarantined download was reported as "damaged" instead of the normal
"unidentified developer" refusal. The build now asks the Tauri bundler for the AD-HOC identity `-` (no hardened runtime, no entitlements,
no timestamp, no keychain). This is not release signing: packaging/signing/** re-signs everything with --force.
"""
import hashlib
import json
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD_LAUNCHER = (ROOT / "packaging/build-launcher.sh").read_text()
CANDIDATE = (ROOT / "packaging/candidate/build-local-macos-candidate.sh").read_text()
CHECK = ROOT / "packaging/candidate/check_adhoc_seal.py"
DARWIN = platform.system() == "Darwin" and shutil.which("codesign") is not None


def test_the_adhoc_override_is_exactly_the_ad_hoc_identity_without_hardened_runtime():
    line = next(l for l in BUILD_LAUNCHER.splitlines() if "--config" in l and "signingIdentity" in l)
    cfg = json.loads(line.split("--config ")[1].strip().rstrip(")").strip("'"))
    assert cfg == {"bundle": {"macOS": {"signingIdentity": "-", "hardenedRuntime": False}}}
    assert "entitlements" not in line.lower()
    assert "LAUNCHER_ADHOC_SIGN" in BUILD_LAUNCHER and '[ "$OS" = macos ]' in BUILD_LAUNCHER
    assert "APPLE_SIGNING_IDENTITY" in BUILD_LAUNCHER  # refuses to run beside a real identity variable


def test_the_committed_tauri_config_is_unchanged_by_the_adhoc_seal():
    mac = json.loads((ROOT / "deploy/launcher/src-tauri/tauri.conf.json").read_text())["bundle"].get("macOS", {})
    assert "signingIdentity" not in mac and "entitlements" not in mac and "hardenedRuntime" not in mac


def test_the_candidate_script_requests_and_checks_the_seal():
    assert "LAUNCHER_ADHOC_SIGN=1" in CANDIDATE
    assert "check_adhoc_seal.py" in CANDIDATE
    assert "codesign --verify --deep --strict" in CANDIDATE  # also on the .app inside the dmg
    assert "fails by design" not in CANDIDATE


def _fixture(tmp: Path, name: str, seal: bool) -> Path:
    app = tmp / name / "Fixture.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Resources/Licenses").mkdir(parents=True)
    # the executable is signed OUTSIDE the bundle first (a stand-in for the linker signature): signing it inside would seal the bundle
    exe = tmp / name / "fixture"
    shutil.copy("/usr/bin/true", exe)
    subprocess.run(["codesign", "--force", "--sign", "-", str(exe)], check=True, capture_output=True)
    shutil.copy(exe, app / "Contents/MacOS/fixture")
    (app / "Contents/Resources/Licenses/LICENSE").write_text("licence text\n")
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "fixture", "CFBundleIdentifier": "io.example.fixture", "CFBundlePackageType": "APPL"}))
    sign = lambda p: subprocess.run(["codesign", "--force", "--sign", "-", str(p)], check=True, capture_output=True)  # noqa: E731
    if seal:
        sign(app)
    return app


@pytest.mark.skipif(not DARWIN, reason="needs macOS codesign")
def test_adhoc_sealing_is_deterministic_and_the_check_separates_sealed_from_unsealed(tmp_path):
    a, b = _fixture(tmp_path, "one", True), _fixture(tmp_path, "two", True)
    h = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    assert h(a / "Contents/_CodeSignature/CodeResources") == h(b / "Contents/_CodeSignature/CodeResources")
    assert h(a / "Contents/MacOS/fixture") == h(b / "Contents/MacOS/fixture")  # same bytes in, same signed bytes out
    ok = subprocess.run([sys.executable, "-I", str(CHECK), str(a)], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout
    unsealed = _fixture(tmp_path, "three", False)
    bad = subprocess.run([sys.executable, "-I", str(CHECK), str(unsealed)], capture_output=True, text=True)
    assert bad.returncode == 1 and "FAIL bundle seals its resources" in bad.stdout
