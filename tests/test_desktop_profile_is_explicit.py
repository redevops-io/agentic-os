"""The packaged desktop sidecar chooses its execution profile explicitly.

Before this, the frozen sidecar started with no ``AGENTIC_OS_PROFILE`` and so ran as ``development`` because a setting was missing. These tests
pin the rule end to end: the profile module refuses an unset profile in a packaged build, the sidecar entry selects ``production`` with durable
per-user stores before the control plane is imported, a stated choice is respected, and a REAL ``serve`` started the way the launcher starts it
reports the production posture with durable authority.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from agentic_os import desktop_profile
from agentic_os.mission import profile as profile_mod

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "packaging" / "sidecar_main.py"


def test_a_packaged_build_with_no_profile_refuses_instead_of_defaulting(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_PROFILE", raising=False)
    monkeypatch.delenv("AGENTIC_OS_REQUIRE_AUTHORITY", raising=False)
    monkeypatch.setenv("AGENTIC_OS_PACKAGED", "1")
    with pytest.raises(profile_mod.ProfileError) as e:
        profile_mod.profile()
    assert "never assumed" in str(e.value)


def test_a_source_checkout_with_no_profile_still_runs_as_development(monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_PROFILE", raising=False)
    monkeypatch.delenv("AGENTIC_OS_REQUIRE_AUTHORITY", raising=False)
    monkeypatch.delenv("AGENTIC_OS_PACKAGED", raising=False)
    assert profile_mod.profile() == "development"


@pytest.mark.parametrize("platform,home,extra,expected_dir", [
    ("darwin", "/Users/u", {}, "/Users/u/Library/Application Support/io.redevops.agentic-apps/authority"),
    ("linux", "/home/u", {}, "/home/u/.local/state/io.redevops.agentic-apps/authority"),
    ("linux", "/home/u", {"XDG_STATE_HOME": "/var/state"}, "/var/state/io.redevops.agentic-apps/authority"),
    ("win32", "C:\\Users\\u", {"LOCALAPPDATA": "C:\\Users\\u\\AppData\\Local"}, "C:\\Users\\u\\AppData\\Local\\io.redevops.agentic-apps\\authority"),
])
def test_the_stores_live_in_the_per_user_state_directory(platform, home, extra, expected_dir):
    assert desktop_profile.state_dir(dict(extra), home, platform) == expected_dir


def test_the_audit_path_override_does_not_move_the_authority_stores():
    env = {"AGENTIC_OS_SOURCES_AUDIT_PATH": "/somewhere/else/audit.jsonl"}
    assert desktop_profile.state_dir(env, "/home/u", "linux") == "/home/u/.local/state/io.redevops.agentic-apps/authority"


def test_nothing_set_selects_production_with_private_durable_stores(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    out = desktop_profile.apply(env, str(tmp_path), "linux")
    d = tmp_path / "io.redevops.agentic-apps" / "authority"
    assert out == {"profile": "production", "chosen_here": True, "authority_db": str(d / "authority.db"), "replay_db": str(d / "replay.db")}
    assert env["AGENTIC_OS_PACKAGED"] == "1" and env["AGENTIC_OS_PROFILE"] == "production"
    assert d.is_dir()
    if os.name == "posix":
        assert (d.stat().st_mode & 0o777) == 0o700


def test_a_stated_development_choice_is_kept_and_gets_no_stores(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path), "AGENTIC_OS_PROFILE": "development"}
    out = desktop_profile.apply(env, str(tmp_path), "linux")
    assert out["profile"] == "development" and out["chosen_here"] is False
    assert "AGENTIC_OS_AUTHORITY_DB" not in env and "AGENTIC_OS_REPLAY_DB" not in env
    assert not (tmp_path / "io.redevops.agentic-apps").exists()


def test_stores_somebody_configured_are_not_replaced(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path), "AGENTIC_OS_PROFILE": "production", "AGENTIC_OS_AUTHORITY_DB": "/data/a.db"}
    out = desktop_profile.apply(env, str(tmp_path), "linux")
    assert out["authority_db"] == "/data/a.db" and out["replay_db"].endswith("replay.db") and out["chosen_here"] is False


def test_the_sidecar_entry_applies_the_profile_before_it_imports_the_control_plane():
    src = ENTRY.read_text(encoding="utf-8")
    apply_at = src.index("_explicit_profile()\n")
    assert apply_at < src.index('runpy.run_module("agentic_os.mission.bootstrap"') and apply_at < src.index("from agentic_os.projects_entry import")
    # help and usage errors must still start nothing and touch nothing
    assert src.index('if action == "error":') < apply_at


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_real_serve_reports_production_with_durable_authority(tmp_path):
    """Start the sidecar entry the way the launcher does (no profile in the environment) and read the posture from a child of the same
    environment: the stores exist on disk and the profile module, given that environment, reports an explicit production posture."""
    port = _free_port()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state"),
           "LOCALAPPDATA": str(tmp_path / "state"), "PYTHONPATH": str(REPO), "PROJECTS_PORT": str(port), "PROJECTS_HOST": "127.0.0.1"}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    log = tmp_path / "serve.log"
    with open(log, "wb") as fh:
        p = subprocess.Popen([sys.executable, str(ENTRY), "serve", "--exit-on-stdin-eof"], env=env, cwd=tmp_path, stdin=subprocess.PIPE,
                             stdout=fh, stderr=subprocess.STDOUT)
    try:
        end = time.time() + 90
        up = False
        while time.time() < end and p.poll() is None:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    up = True
                    break
            except OSError:
                time.sleep(0.2)
        assert up, "the sidecar did not listen under the production profile:\n" + log.read_text(errors="replace")[-3000:]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            assert r.status == 200
        assert "redevops-sidecar: profile=production; authority and replay stores are durable, per user" in log.read_text(errors="replace")
        probe = subprocess.run([sys.executable, "-c", (
            "import json, os\n"
            "from agentic_os import desktop_profile\n"
            "desktop_profile.apply()\n"
            "from agentic_os.mission import profile\n"
            "from agentic_os.mission.authority import authority_from_env\n"
            "from agentic_os.mission.approval import default_replay_guard\n"
            "g = authority_from_env(); default_replay_guard()\n"
            "print(json.dumps({'st': profile.status(g, replay_durable=True), 'a': os.environ['AGENTIC_OS_AUTHORITY_DB'], 'r': os.environ['AGENTIC_OS_REPLAY_DB']}))\n")],
            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert probe.returncode == 0, probe.stderr[-2000:]
        out = json.loads(probe.stdout.strip().splitlines()[-1])
        st = out["st"]
        assert st["profile"] == "production" and st["explicit"] is True and st["authority_durable"] is True and st["warnings"] == [], st
        assert Path(out["a"]).exists() and Path(out["r"]).exists()
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()


def test_the_dashboards_and_the_intake_get_durable_per_user_locations_and_a_configured_one_is_kept(tmp_path):
    env = {"HOME": str(tmp_path)}
    desktop_profile.apply(env, home=str(tmp_path), platform="linux")
    d = desktop_profile.state_dir(env, str(tmp_path), "linux")
    assert env["AGENTIC_OS_DASHBOARDS_DIR"] == d + "/dashboards" and os.path.isdir(env["AGENTIC_OS_DASHBOARDS_DIR"])
    assert env["AGENTIC_OS_CAPTURE_DB"] == d + "/capture.db"
    mine = {"HOME": str(tmp_path), "AGENTIC_OS_DASHBOARDS_DIR": "/srv/dash", "AGENTIC_OS_CAPTURE_DB": "/srv/capture.db"}
    desktop_profile.apply(mine, home=str(tmp_path), platform="linux")
    assert mine["AGENTIC_OS_DASHBOARDS_DIR"] == "/srv/dash" and mine["AGENTIC_OS_CAPTURE_DB"] == "/srv/capture.db"


def test_a_stated_development_choice_gets_no_dashboard_or_intake_location(tmp_path):
    env = {"HOME": str(tmp_path), "AGENTIC_OS_PROFILE": "development"}
    desktop_profile.apply(env, home=str(tmp_path), platform="linux")
    assert "AGENTIC_OS_DASHBOARDS_DIR" not in env and "AGENTIC_OS_CAPTURE_DB" not in env
