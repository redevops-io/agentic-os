"""rel/macos: the signing script accepts the build's AD-HOC seal in its input bundle (and nothing else in Contents/_CodeSignature).

The private candidate is sealed ad-hoc by its build (LAUNCHER_ADHOC_SIGN=1) so a quarantined copy is not reported as "damaged". The reviewed signing
script used to refuse any Contents/_CodeSignature ("nested code we would not know to sign"), so the sealed artifact could not be signed. The script now
removes exactly an ad-hoc `CodeResources` seal from its STAGED copy and replaces it with the real signing (--force); anything else is still refused.
"""
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_signing_procedure_r0prime import APP_NAME, DMG_NAME, IDENT, MAC, R0, SIGN, clean_env, run, sha_file

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS tools")


def _sealed_dmg(tmp_path: Path, extra_seal_file: bool = False, seal: bool = True):
    cc = shutil.which("clang")
    if not cc or not shutil.which("hdiutil") or not shutil.which("codesign"):
        pytest.skip("clang/hdiutil/codesign not available")
    app = tmp_path / "srcapp" / APP_NAME
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources" / "Licenses").mkdir(parents=True)
    for n, code in ((R0["main_binary_name"], 0), (R0["sidecar_binary_name"], 7)):
        src = tmp_path / f"{n}.c"
        src.write_text(f"int main(void){{return {code};}}\n")
        r = subprocess.run([cc, "-arch", "arm64", "-mmacosx-version-min=11.0", "-o", str(app / "Contents" / "MacOS" / n), str(src)], capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip("cannot compile an arm64 fixture: " + r.stderr[:200])
    info = {"CFBundleExecutable": R0["main_binary_name"], "CFBundleIdentifier": R0["bundle_identifier"], "CFBundleName": R0["product_name"],
            "CFBundleShortVersionString": R0["version"], "CFBundleVersion": R0["bundle_version"], "LSMinimumSystemVersion": "11.0",
            "RedevopsReleaseLine": IDENT["release_line"], "CFBundlePackageType": "APPL"}
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    (app / "Contents" / "Resources" / "icon.icns").write_bytes(b"icns")
    for n in ("LICENSE", "LICENSING.md", "THIRD_PARTY_NOTICES.txt"):
        (app / "Contents" / "Resources" / "Licenses" / n).write_text(n + "\n")
    if seal:  # what the build does: sidecar and launcher, then the bundle, identity "-"
        for part in (app / "Contents/MacOS" / R0["sidecar_binary_name"], app / "Contents/MacOS" / R0["main_binary_name"], app):
            subprocess.run(["codesign", "--force", "--sign", "-", str(part)], check=True, capture_output=True)
        assert (app / "Contents/_CodeSignature/CodeResources").is_file()
    if extra_seal_file:
        (app / "Contents/_CodeSignature/planted.dylib").write_bytes(b"x")
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "signing"
    work.mkdir()
    r = run(["/bin/bash", SIGN / "tools" / "make_adhoc_fixture.sh", app, work / "fx"], clean_env(home))
    assert r.returncode == 0, r.stderr
    return home, work, work / "fx" / "input" / DMG_NAME


def test_an_adhoc_sealed_input_is_accepted_and_resealed(tmp_path):
    home, work, dmg = _sealed_dmg(tmp_path)
    out = work / "out"
    r = run(["/bin/bash", MAC, "--adhoc-test", "--input-dmg", dmg, "--expected-sha256", sha_file(dmg), "--out-dir", out], clean_env(home), timeout=600)
    assert r.returncode == 0, (r.stdout[-3000:], r.stderr)
    assert "AD-HOC seal" in (out / "EVIDENCE.txt").read_text()
    t = (out / "codesign" / "bundle.dvvv.txt").read_text()
    assert "runtime" in t and "Sealed Resources version=2" in t


def test_an_unsealed_input_is_still_accepted(tmp_path):
    home, work, dmg = _sealed_dmg(tmp_path, seal=False)
    r = run(["/bin/bash", MAC, "--adhoc-test", "--input-dmg", dmg, "--expected-sha256", sha_file(dmg), "--out-dir", work / "out"], clean_env(home), timeout=600)
    assert r.returncode == 0, (r.stdout[-3000:], r.stderr)


def test_anything_else_in_code_signature_is_still_refused(tmp_path):
    home, work, dmg = _sealed_dmg(tmp_path, extra_seal_file=True)
    r = run(["/bin/bash", MAC, "--adhoc-test", "--input-dmg", dmg, "--expected-sha256", sha_file(dmg), "--out-dir", work / "out"], clean_env(home), timeout=600)
    assert r.returncode != 0
    assert "only the ad-hoc seal's CodeResources is accepted" in (r.stdout + r.stderr)
