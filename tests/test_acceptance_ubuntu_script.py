"""LNX-02: packaging/acceptance-ubuntu.sh must hand apt-get the .deb as a real path whatever form the caller gives it.

The script used `apt-get install -y "./$DEB"`: with an absolute $DEB that is `.//abs/x.deb`, which does not exist; apt failed
with its stderr discarded and the run fell back to `dpkg -i` plus `apt-get -f install`, a different install path from the one
the acceptance claims to exercise. This test runs the real script with fake sudo/apt-get/dpkg executables (no root, no
network, no package is installed) and checks the argument apt-get received."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "acceptance-ubuntu.sh"


def _fake_bin(tmp: Path) -> Path:
    b = tmp / "bin"
    b.mkdir()
    (b / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    (b / "apt-get").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tmp}/apt-args"\nexit 0\n')
    (b / "dpkg").write_text(f'#!/bin/sh\necho dpkg-called > "{tmp}/dpkg-called"\nexit 1\n')
    for f in b.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return b


def _run(tmp: Path, deb_arg: str, cwd: Path) -> list[str]:
    env = {"PATH": f"{_fake_bin(tmp)}:/usr/bin:/bin", "HOME": str(tmp), "LANG": "C"}
    r = subprocess.run(["bash", str(SCRIPT), deb_arg], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    assert not (tmp / "dpkg-called").exists(), "apt-get succeeded, dpkg -i must not be reached"
    assert (tmp / "apt-args").exists(), r.stdout + r.stderr
    return (tmp / "apt-args").read_text().splitlines()


def test_an_absolute_path_with_spaces_reaches_apt_as_that_absolute_path(tmp_path):
    deb = tmp_path / "dir with space" / "Agentic Apps R0-prime_0.1.1_arm64.deb"
    deb.parent.mkdir()
    deb.write_bytes(b"x")
    args = _run(tmp_path, str(deb), tmp_path)
    assert args == ["install", "-y", str(deb)], args


def test_a_relative_path_is_made_absolute(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.deb").write_bytes(b"x")
    args = _run(tmp_path, "a/x.deb", tmp_path)
    assert args == ["install", "-y", str(tmp_path / "a" / "x.deb")], args


def test_a_missing_file_fails_before_anything_is_installed(tmp_path):
    env = {"PATH": f"{_fake_bin(tmp_path)}:/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C"}
    r = subprocess.run(["bash", str(SCRIPT), str(tmp_path / "nope.deb")], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "is not a file" in r.stdout
    assert not (tmp_path / "apt-args").exists()
