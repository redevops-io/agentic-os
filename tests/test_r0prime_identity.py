"""R0-prime identity (owner decision 8): distinct names, versions and identifiers from the frozen v0.3.2 deliverable.

`packaging/identity.json` is the source of truth. These tests keep tauri.conf.json, Cargo.toml/Cargo.lock, Info.plist, the launcher
window text, the build scripts, the build manifest, both acceptance kits and docs/R0_PRIME_IDENTITY.md equal to it, assert that every
identity-bearing value differs from the frozen one, and check the rules of the target formats (Apple version strings, MSI ProductVersion,
the Windows UpgradeCode). No GUI, no Rust toolchain, no network.
"""
from __future__ import annotations

import json
import plistlib
import re
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PK = ROOT / "packaging"
CONF_DIR = ROOT / "deploy" / "launcher" / "src-tauri"
sys.path.insert(0, str(PK / "tools"))
import check_bundle_identity as cbi  # noqa: E402
import identity as ident  # noqa: E402

RAW = json.loads((PK / "identity.json").read_text(encoding="utf-8"))
NEW = ident.resolve(False)
OLD = ident.resolve(True)
CONF = json.loads((CONF_DIR / "tauri.conf.json").read_text(encoding="utf-8"))

# What the frozen v0.3.2 deliverable is. Recorded, never to change (the frozen candidate is read-only).
FROZEN_FACTS = {
    "product_name": "Agentic Apps", "version": "0.1.0", "bundle_identifier": "io.redevops.agentic-apps",
    "main_binary_name": "agentic-apps", "dmg_macos_arm64": "Agentic Apps_0.1.0_aarch64.dmg",
    "msi_x64": "Agentic Apps_0.1.0_x64_en-US.msi", "nsis_x64": "Agentic Apps_0.1.0_x64-setup.exe",
    "app_bundle": "Agentic Apps.app",
}


# ------------------------------------------------------------------------------------------------ the data
def test_frozen_identity_is_recorded_exactly():
    for k, v in FROZEN_FACTS.items():
        assert OLD[k] == v, (k, OLD[k])
    assert OLD["wix_upgrade_code"] == ident.wix_default_upgrade_code("Agentic Apps")  # what Tauri derived for the frozen build


def test_every_identity_bearing_value_differs_from_the_frozen_one():
    for k in ("product_name", "version", "bundle_version", "bundle_identifier", "sidecar_signing_identifier", "main_binary_name",
              "wix_upgrade_code", "app_bundle", "dmg_macos_arm64", "msi_x64", "nsis_x64", "launcher_exe_windows", "dmg_volume_name",
              "install_dir_registry_key", "msi_install_dir_name", "webview_and_cache_dir_name", "nsis_uninstall_key"):
        assert NEW[k] != OLD[k], k
    assert NEW["release_line"] == "r0-prime" and OLD["release_line"] != NEW["release_line"]


def test_what_is_intentionally_unchanged():
    assert NEW["sidecar_binary_name"] == OLD["sidecar_binary_name"] == "redevops-sidecar"
    # the publisher (second identifier element) is shared, so only the product name keeps the registry keys apart
    assert NEW["bundle_identifier"].split(".")[:3] == OLD["bundle_identifier"].split(".")[:3]


def test_new_identifier_is_a_valid_distinct_tauri_identifier():
    new, old = NEW["bundle_identifier"], OLD["bundle_identifier"]
    assert re.fullmatch(r"[A-Za-z0-9.-]+", new)                 # Tauri: alphanumerics, hyphens, periods
    assert not new.endswith(".app")
    assert new.startswith(old + ".")                             # one family; the frozen id is a prefix, never equal
    assert NEW["sidecar_signing_identifier"] == new + ".sidecar"


def test_executable_name_fits_the_process_name_limit_and_the_old_name_is_not_a_prefix_match():
    assert len(NEW["main_binary_name"]) <= 15                    # Linux comm length; pgrep -x / killall by name
    assert NEW["main_binary_name"] != OLD["main_binary_name"] and not NEW["main_binary_name"].startswith(OLD["main_binary_name"])


# ------------------------------------------------------------------------------------------------ version formats
def test_version_is_valid_for_apple_msi_and_nsis_without_a_prerelease_form():
    for v in (NEW["version"], NEW["bundle_version"]):
        assert ident.APPLE_VERSION.match(v), v                   # CFBundleShortVersionString / CFBundleVersion: 1-3 integers
    major, minor, patch = (int(x) for x in NEW["version"].split("."))
    assert major <= 255 and minor <= 255 and patch <= 65535      # MSI ProductVersion field limits
    assert "-" not in NEW["version"] and "+" not in NEW["version"]   # no pre-release / build metadata (see docs/R0_PRIME_IDENTITY.md section 2)
    assert tuple(int(x) for x in NEW["version"].split(".")) > tuple(int(x) for x in OLD["version"].split("."))


def test_msi_upgrade_code_is_a_valid_distinct_guid_equal_to_the_derivation_for_the_new_name():
    code = NEW["wix_upgrade_code"]
    assert str(uuid.UUID(code)) == code
    assert code != OLD["wix_upgrade_code"]
    assert code == ident.wix_default_upgrade_code(NEW["product_name"])
    assert CONF["bundle"]["windows"]["wix"]["upgradeCode"] == code


# ------------------------------------------------------------------------------------------------ the files agree
def test_tauri_conf_matches_identity():
    assert CONF["productName"] == NEW["product_name"]
    assert CONF["version"] == NEW["version"]
    assert CONF["identifier"] == NEW["bundle_identifier"]
    assert CONF["mainBinaryName"] == NEW["main_binary_name"]
    assert CONF["bundle"]["macOS"]["bundleVersion"] == NEW["bundle_version"]
    assert CONF["app"]["windows"][0]["title"] == NEW["product_name"]
    assert CONF["bundle"]["externalBin"] == ["binaries/" + NEW["sidecar_binary_name"]]


def test_cargo_manifest_lock_and_target_lists_follow_the_product_version():
    cargo = tomllib.loads((CONF_DIR / "Cargo.toml").read_text(encoding="utf-8"))
    assert cargo["package"]["version"] == NEW["version"]
    lock = tomllib.loads((CONF_DIR / "Cargo.lock").read_text(encoding="utf-8"))
    row = [p for p in lock["package"] if p["name"] == "redevops-launcher"]
    assert len(row) == 1 and row[0]["version"] == NEW["version"]
    for os_ in ("macos", "windows", "linux"):
        text = (PK / "cargo-targets" / f"compiled-crates-{os_}.txt").read_text(encoding="utf-8")
        assert f"redevops-launcher v{NEW['version']}" in text and "redevops-launcher v0.1.0" not in text


def test_info_plist_carries_the_release_line_marker_and_nothing_that_changes_behaviour():
    data = plistlib.loads((CONF_DIR / "Info.plist").read_bytes())
    assert data == {"RedevopsReleaseLine": RAW["release_line"]}


def test_launcher_window_shows_the_release_line_as_static_text():
    html = (ROOT / "deploy" / "launcher" / "ui" / "index.html").read_text(encoding="utf-8")
    assert f"release line {RAW['release_line']}" in html
    assert 'id="release-line"' in html and "<script" in html          # text only: the invoke() commands are unchanged (script is the external app.js since RC3)
    assert "<title>Agentic Apps R0-prime</title>" in html and "<h1>Agentic Apps R0-prime</h1>" in html and "ReDevOps" not in html
    assert re.search(r'<p class="sub" id="release-line">[^<]*</p>', html)


def test_build_manifest_records_the_release_line_and_identity():
    import build_manifest as bm
    line, info = bm.release_identity(ROOT)
    assert line == "r0-prime"
    assert info["product_name"] == NEW["product_name"] and info["bundle_identifier"] == NEW["bundle_identifier"]
    assert info["version"] == NEW["version"] and info["main_binary_name"] == NEW["main_binary_name"]
    assert "packaging/identity.json" in bm.INPUT_GLOBS and "deploy/launcher/src-tauri/Info.plist" in bm.INPUT_GLOBS
    assert bm.release_identity(Path("/nonexistent-root-for-the-test")) == (None, None)


def test_verify_pins_expects_the_new_launcher_crate_version():
    text = (PK / "verify-pins.py").read_text(encoding="utf-8")
    assert f'("redevops-launcher", "{NEW["version"]}")' in text


# ------------------------------------------------------------------------------------------------ the build scripts follow
@pytest.mark.parametrize("script", ["build-launcher.sh", "build-sbom.sh", "verify-reproducible.sh"])
def test_build_scripts_take_the_launcher_binary_name_from_the_identity(script):
    text = (PK / script).read_text(encoding="utf-8")
    assert "identity main_binary_name" in text
    assert "release/agentic-apps" not in text


def test_candidate_script_uses_the_identity_and_checks_the_bundle():
    text = (PK / "candidate" / "build-local-macos-candidate.sh").read_text(encoding="utf-8")
    assert 'APPNAME="$(ident app_bundle)"' in text and "check_bundle_identity.py" in text
    assert 'APPNAME="Agentic Apps.app"' not in text and "Contents/MacOS/agentic-apps" not in text
    assert "listings/identity-check.txt" in text


def test_notices_sbom_and_source_offer_use_the_product_name_from_the_config():
    assert "identity.py" in (PK / "notices" / "build-licenses.sh").read_text(encoding="utf-8")
    assert 'f"{product_name} (native build)"' in (PK / "sbom" / "generate_sbom.py").read_text(encoding="utf-8")
    assert "release line" in (PK / "sbom" / "generate_source_offer.py").read_text(encoding="utf-8")


def test_identity_cli():
    def run(*a):
        return subprocess.run([sys.executable, "-I", str(PK / "tools" / "identity.py"), *a], capture_output=True, text=True)
    r = run("get", "dmg_macos_arm64")
    assert r.returncode == 0 and r.stdout == NEW["dmg_macos_arm64"] + "\n"
    r = run("--frozen", "get", "dmg_macos_arm64")
    assert r.returncode == 0 and r.stdout == FROZEN_FACTS["dmg_macos_arm64"] + "\n"
    assert json.loads(run("all").stdout)["release_line"] == "r0-prime"
    assert run("get", "no_such_key").returncode == 2 and run().returncode == 2


# ------------------------------------------------------------------------------------------------ the bundle checker
def _fake_bundle(tmp: Path, line: dict, **over) -> Path:
    app = tmp / line["app_bundle"]
    (app / "Contents" / "MacOS").mkdir(parents=True)
    plist = {"CFBundleIdentifier": line["bundle_identifier"], "CFBundleShortVersionString": line["version"],
             "CFBundleVersion": line["bundle_version"], "CFBundleName": line["product_name"], "CFBundleDisplayName": line["product_name"],
             "CFBundleExecutable": line["main_binary_name"]}
    if line is NEW:
        plist["RedevopsReleaseLine"] = "r0-prime"
    plist.update(over)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(plist))
    (app / "Contents" / "MacOS" / line["main_binary_name"]).write_bytes(b"x")
    (app / "Contents" / "MacOS" / line["sidecar_binary_name"]).write_bytes(b"x")
    return app


def _failed(rows):
    return [r[1] for r in rows if r[0] == "FAIL"]


def test_checker_accepts_the_r0prime_bundle_and_dmg_name(tmp_path):
    app = _fake_bundle(tmp_path, NEW)
    assert _failed(cbi.check(app, tmp_path / NEW["dmg_macos_arm64"], False)) == []
    assert cbi.main(["--app", str(app), "--dmg", str(tmp_path / NEW["dmg_macos_arm64"])]) == 0


def test_checker_refuses_the_frozen_bundle_as_r0prime_and_accepts_it_as_frozen(tmp_path):
    app = _fake_bundle(tmp_path, OLD)
    failed = _failed(cbi.check(app, tmp_path / OLD["dmg_macos_arm64"], False))
    assert {"CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion", "CFBundleExecutable", "app bundle file name",
            "dmg file name", "RedevopsReleaseLine"} <= set(failed)
    assert _failed(cbi.check(app, tmp_path / OLD["dmg_macos_arm64"], True)) == []


@pytest.mark.parametrize("over,expect", [
    ({"CFBundleIdentifier": "io.redevops.agentic-apps"}, "CFBundleIdentifier"),
    ({"CFBundleShortVersionString": "0.1.1-r0prime.1"}, "CFBundleShortVersionString is 1-3 integers"),
    ({"CFBundleVersion": "0.1.0"}, "CFBundleVersion"),
    ({"CFBundleVersion": "1.2.3.4"}, "CFBundleVersion is 1-3 integers"),
    ({"RedevopsReleaseLine": "r0"}, "RedevopsReleaseLine"),
    ({"CFBundleDisplayName": "Agentic Apps"}, "CFBundleDisplayName"),
    ({"CFBundleExecutable": "agentic-apps"}, "CFBundleExecutable"),
])
def test_checker_fails_on_each_wrong_value(tmp_path, over, expect):
    app = _fake_bundle(tmp_path, NEW, **over)
    assert expect in _failed(cbi.check(app, None, False))
    assert cbi.main(["--app", str(app)]) == 1


def test_checker_fails_on_the_frozen_dmg_name_and_a_missing_plist(tmp_path):
    app = _fake_bundle(tmp_path, NEW)
    assert "dmg file name" in _failed(cbi.check(app, tmp_path / FROZEN_FACTS["dmg_macos_arm64"], False))
    empty = tmp_path / "empty" / NEW["app_bundle"]
    empty.mkdir(parents=True)
    assert "Info.plist" in _failed(cbi.check(empty, None, False))


# ------------------------------------------------------------------------------------------------ the kits follow
MAC = PK / "acceptance" / "macos-r0prime"
WIN = PK / "acceptance" / "windows-r0prime"


def _shell_var(text: str, name: str) -> str:
    m = re.search(rf'^{name}="([^"]*)"', text, re.M)
    assert m, name
    return m.group(1)


def test_macos_kit_literals_equal_the_identity():
    lib = (MAC / "_lib.sh").read_text(encoding="utf-8")
    assert _shell_var(lib, "APP_ID") == NEW["bundle_identifier"]
    assert _shell_var(lib, "APP_NAME") == NEW["app_bundle"]
    assert _shell_var(lib, "MAIN_EXE") == NEW["main_binary_name"]
    assert _shell_var(lib, "APP_VERSION") == NEW["version"]
    assert _shell_var(lib, "RELEASE_LINE") == RAW["release_line"]
    assert _shell_var(lib, "FROZEN_APP_ID") == OLD["bundle_identifier"]
    assert _shell_var(lib, "FROZEN_APP_NAME") == OLD["app_bundle"]
    assert _shell_var(lib, "FROZEN_MAIN_EXE") == OLD["main_binary_name"]
    assert _shell_var(lib, "FROZEN_APP_VERSION") == OLD["version"]


def _ps_var(text: str, name: str) -> str:
    m = re.search(rf"^\$script:{name} = '([^']*)'", text, re.M)
    assert m, name
    return m.group(1)


def test_windows_kit_literals_equal_the_identity():
    common = (WIN / "Common.ps1").read_text(encoding="ascii")
    assert _ps_var(common, "ProductName") == NEW["product_name"]
    assert _ps_var(common, "LauncherExe") == NEW["launcher_exe_windows"]
    assert _ps_var(common, "LauncherProcess") == NEW["main_binary_name"]
    assert _ps_var(common, "AppId") == NEW["bundle_identifier"]
    assert _ps_var(common, "AppVersion") == NEW["version"]
    assert _ps_var(common, "ManufacturerKey") == "HKCU:\\" + NEW["install_dir_registry_key"].split("\\", 1)[1]
    assert _ps_var(common, "FrozenProductName") == OLD["product_name"]
    assert _ps_var(common, "FrozenLauncherExe") == OLD["launcher_exe_windows"]
    assert _ps_var(common, "FrozenAppId") == OLD["bundle_identifier"]
    assert _ps_var(common, "FrozenManufacturerKey") == "HKCU:\\" + OLD["install_dir_registry_key"].split("\\", 1)[1]


def _kit_files():
    files = [p for p in MAC.iterdir() if p.suffix in (".sh", ".md")]
    files += [p for p in WIN.rglob("*") if p.is_file() and p.suffix in (".ps1", ".sh", ".md")]
    return sorted(files)


def test_kits_do_not_use_the_frozen_names_except_where_they_are_named_as_frozen():
    """A bare frozen name in a kit is a stale copy: it must sit on a line that says FROZEN / v0.3.2 / frozen, or be a leftover-scan glob."""
    patterns = [r"(?<![\w.-])agentic-apps(?![\w.-])", r"Agentic Apps\.app", r"Agentic Apps_0\.1\.0", r"Agentic\.Apps_0\.1\.0",
                r"(?<![\w.-])io\.redevops\.agentic-apps(?![\w.-])", r"\\Agentic Apps(?! R0-prime)['\"\\ ]"]
    bad = []
    for f in _kit_files():
        for n, line in enumerate(f.read_text(encoding="ascii").splitlines(), 1):
            if not any(re.search(p, line) for p in patterns):
                continue
            if re.search(r"frozen|v0\.3\.2|-iname|name \"\$FROZEN", line, re.I):
                continue
            bad.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()[:140]}")
    assert not bad, "\n".join(bad)


def test_kit_docs_and_scripts_name_the_new_executable_and_bundle():
    text = "\n".join(p.read_text(encoding="ascii") for p in _kit_files())
    for needle in (NEW["main_binary_name"], NEW["app_bundle"], NEW["product_name"], NEW["bundle_identifier"]):
        assert needle in text, needle


def test_verify_dmg_checks_the_identity_rows():
    text = (MAC / "verify_dmg.sh").read_text(encoding="ascii")
    for row in ("P-06", "P-07", "P-08", "P-09"):
        assert f"rec PASS {row}" in text and f"rec FAIL {row}" in text
    assert "RedevopsReleaseLine" in text


def test_post_run_checks_guards_the_frozen_identity():
    text = (MAC / "post_run_checks.sh").read_text(encoding="ascii")
    assert "LB-04" in text and "$FROZEN_APP_ID" in text and "agentic-r0prime" in text


# ------------------------------------------------------------------------------------------------ the documentation
def test_identity_document_states_every_distinct_value_and_how_to_tell_the_lines_apart():
    doc = (ROOT / "docs" / "R0_PRIME_IDENTITY.md").read_text(encoding="utf-8")
    for key in ("product_name", "version", "bundle_identifier", "sidecar_signing_identifier", "main_binary_name", "wix_upgrade_code",
                "app_bundle", "dmg_macos_arm64", "msi_x64", "nsis_x64"):
        assert NEW[key] in doc, key
        assert OLD[key] in doc or key in ("app_bundle",), key
    for needle in ("release_line", "r0-prime", "RedevopsReleaseLine", "intentionally unchanged", "Compatibility risks", "check_bundle_identity.py",
                   "~/.agentic-os"):
        assert needle in doc, needle
    assert "Agentic.Apps_0.1.0_aarch64.dmg" in doc          # the frozen release file name is named as such


def test_no_document_claims_the_r0prime_bundle_is_the_frozen_identity():
    for rel in ("docs/LOCAL_API_SECURITY.md", "docs/MACOS_ENTITLEMENTS.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "io.redevops.agentic-apps/launch" not in text, rel                       # the cache directory is the R0-prime one
        assert "Agentic Apps.app/Contents" not in text and "Contents/MacOS/agentic-apps`" not in text, rel
