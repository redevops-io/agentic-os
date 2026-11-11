"""The native build drivers: syntax, the guards they must keep, and that they never touch a credential, sign, push or publish."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ND = ROOT / "scripts" / "release" / "native"


def scripts():
    return sorted(p for p in ND.iterdir() if p.suffix == ".sh")


def test_every_driver_script_parses():
    assert scripts()
    for s in scripts():
        assert subprocess.run(["bash", "-n", str(s)], capture_output=True).returncode == 0, s.name


def test_drivers_neither_sign_push_tag_nor_read_credentials():
    banned = [r"\bgit\s+push\b", r"\bgit\s+tag\b", r"codesign", r"notarytool", r"ssh-keygen", r"\.ssh", r"az\s+login", r"R0P_", r"keychain", r"jsign", r"curl[^|\n]*-X\s*(PUT|POST)"]
    for s in scripts() + [ND / "lima-linux.yaml"]:
        code = "\n".join(l for l in s.read_text().splitlines() if not l.lstrip().startswith("#"))
        for pat in banned:
            assert not re.search(pat, code), (s.name, pat)


def test_the_linux_build_is_pinned_clean_and_uses_only_the_two_documented_url_rewrites():
    b = (ND / "vm_build.sh").read_text()
    assert "SOURCE MISMATCH" in b and "DIRTY" in b and "env -i" in b and "git checkout -q --detach" in b
    assert b.count("insteadOf") == 2 and "redevops-connectors" in b and "knowledge-frontier" in b
    assert "runtime-contracts" not in b.split("GIT_CONFIG_COUNT", 1)[1] and "discovery-runtime" not in b.split("GIT_CONFIG_COUNT", 1)[1]
    y = (ND / "lima-linux.yaml").read_text()
    assert "sha256:1d6bffe64b848468ac97f821d369a4846d983de1800ccf6b5ec8853e85cefc55" in y and "mounts: null" in y and "release-20260926" in y


def test_the_driver_refuses_a_dirty_repository_an_existing_output_and_a_ref_that_is_not_the_commit():
    d = (ND / "linux_run.sh").read_text()
    for needle in ("REFUSED: $OUT exists", "REFUSED: $REF is not $COMMIT", "REFUSED: the repository is not clean", "git bundle verify", "trap cleanup EXIT"):
        assert needle in d, needle
