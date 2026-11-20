"""Static guards for the Snap Store draft (snap/snapcraft.yaml, docs/release/SNAP_STORE.md). No snapcraft, no VM.

The snap's permission set is a security decision: a change to it is an owner decision (docs/release/SNAP_STORE.md section 2), so
these tests pin it and fail on any widening until the test and the document are changed together on purpose."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SNAP = yaml.safe_load((ROOT / "snap" / "snapcraft.yaml").read_text(encoding="utf-8"))
WRAPPER = (ROOT / "snap" / "local" / "agentic-apps-snap").read_text(encoding="utf-8")
BUILDER = (ROOT / "snap" / "local" / "build-payload.sh").read_text(encoding="utf-8")


def test_confinement_is_strict_and_the_grade_is_devel():
    assert SNAP["confinement"] == "strict"
    assert SNAP["grade"] == "devel", "a stable grade is the owner's decision when a store release is wanted"
    assert SNAP["base"] == "core24"


def test_the_only_declared_interfaces_are_network_network_bind_and_docker():
    apps = SNAP["apps"]
    assert len(apps) == 1
    (app,) = apps.values()
    # docker added 2026-11-20, owner decision O-SNAP-2 (docs/release/SNAP_STORE.md) - super-privileged, not
    # auto-connected, needs a Store manual-review grant before `snap connect railsapps:docker` can succeed.
    assert sorted(app["plugs"]) == ["docker", "network", "network-bind"], app["plugs"]
    assert app["extensions"] == ["gnome"], "the gnome extension adds desktop/wayland/x11/opengl/gsettings and nothing privileged"
    assert "plugs" not in SNAP or not SNAP["plugs"], "no top-level plug declarations"
    assert "slots" not in SNAP and "hooks" not in SNAP


def test_no_other_privileged_interface_or_classic_anywhere_in_the_snap_sources():
    """docker is the one deliberate exception (test above); every other privileged interface or classic
    confinement must still never appear anywhere in this file, by accident or otherwise."""
    text = (ROOT / "snap" / "snapcraft.yaml").read_text(encoding="utf-8")
    for forbidden in ("classic", "home:", "removable-media", "system-files", "ssh-keys", "process-control",
                      "password-manager-service", "personal-files", "devmode", "login-session-control"):
        # the header comment names what is NOT requested; only non-comment lines count
        live = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        assert not any(forbidden in ln for ln in live), forbidden


def test_the_wrapper_and_builder_do_not_escalate():
    for name, body in (("wrapper", WRAPPER), ("builder", BUILDER)):
        for forbidden in ("sudo", "docker", "chmod 777", "setcap", "pkexec"):
            assert forbidden not in body, f"{name} mentions {forbidden}"
    assert "umask 077" in WRAPPER and "SNAP_USER_COMMON" in WRAPPER
    assert "WEBKIT_DISABLE_SANDBOX" not in WRAPPER + BUILDER + (ROOT / "snap" / "snapcraft.yaml").read_text(encoding="utf-8"), (
        "disabling the WebKit sandbox is an owner decision (SNAP_STORE.md), not applied")


def test_the_snap_builds_both_architectures_and_is_named_for_the_owners_registered_store_name():
    assert set(SNAP["platforms"]) == {"amd64", "arm64"}
    assert "license" not in SNAP, "the family licence has no SPDX id; the listing licence field is an owner decision"
    # registered by the owner on the Snap Store (2026-11-19); the app key under `apps:` matches so the
    # installed command is plain `railsapps`, not `railsapps.<something>`
    assert SNAP["name"] == "railsapps"
    assert set(SNAP["apps"]) == {"railsapps"}
