"""The macOS entitlement input for the later signing run: one key, scoped to the sidecar, nothing else carries it."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLIST = ROOT / "packaging" / "macos" / "sidecar.entitlements.plist"
KEY = "com.apple.security.cs.disable-library-validation"


def test_the_sidecar_entitlements_contain_exactly_disable_library_validation():
    data = plistlib.loads(PLIST.read_bytes())
    assert data == {KEY: True}, data          # no other key, not even a false one


def test_the_plist_is_named_and_documented_for_the_sidecar_only():
    assert PLIST.name == "sidecar.entitlements.plist"
    assert not list((ROOT / "packaging" / "macos").glob("launcher*")), "the launcher carries no entitlements"
    doc = (ROOT / "docs" / "MACOS_ENTITLEMENTS.md").read_text(encoding="utf-8")
    assert "Contents/MacOS/redevops-sidecar" in doc and "residual risk" in doc.lower()
    assert "library planting" in doc.lower()


def test_no_launcher_or_bundle_configuration_applies_an_entitlements_file():
    conf = json.loads((ROOT / "deploy/launcher/src-tauri/tauri.conf.json").read_text())
    mac = conf["bundle"].get("macOS", {})
    assert "entitlements" not in mac and "signingIdentity" not in mac and "providerShortName" not in mac
    assert "entitlements" not in json.dumps(conf).lower()
    assert not list((ROOT / "deploy/launcher/src-tauri").glob("*.entitlements*"))
    assert not list((ROOT / "deploy/launcher/src-tauri").glob("Entitlements.plist"))


def test_only_the_plist_and_its_documentation_name_the_entitlement():
    r = subprocess.run(["git", "-C", str(ROOT), "grep", "-l", KEY], capture_output=True, text=True)
    files = set(r.stdout.split())
    # this test and the docs describe it; the plist carries it; the licence/decision registers may cite it
    allowed = {"packaging/macos/sidecar.entitlements.plist", "docs/MACOS_ENTITLEMENTS.md",
               "tests/test_macos_entitlements.py", "tests/test_signing_procedure_r0prime.py",
               # the macOS acceptance kit READS entitlements to verify them (no signing, no writing of the key)
               "packaging/acceptance/macos-r0prime/SIGNING-READINESS.md", "packaging/acceptance/macos-r0prime/verify_dmg.sh"}
    # the macOS acceptance kit (incl. the gui harness) only READS/verifies the key on COPIES; it never writes it into the repository plist
    # packaging/signing/ (the prepared signing scripts and documents) READS the entitlement back to verify it and passes the repository plist to the sidecar signing only
    extra = {f for f in files if f not in allowed and not f.startswith(("packaging/DECISIONS", "reports/", "packaging/acceptance/macos-r0prime/", "packaging/signing/"))}
    assert not extra, extra


def test_minimum_system_version_matches_the_arm64_binaries():
    conf = json.loads((ROOT / "deploy/launcher/src-tauri/tauri.conf.json").read_text())
    assert conf["bundle"]["macOS"]["minimumSystemVersion"] == "11.0"


@pytest.mark.skipif(not os.environ.get("FROZEN_SIDECAR_ENTITLEMENTS"), reason="set FROZEN_SIDECAR_ENTITLEMENTS to the frozen v0.3.2 plist")
def test_equal_in_intent_to_the_frozen_plist():
    frozen = plistlib.loads(Path(os.environ["FROZEN_SIDECAR_ENTITLEMENTS"]).read_bytes())
    assert plistlib.loads(PLIST.read_bytes()) == frozen
