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


def test_the_windows_driver_never_logs_in_opens_no_port_prints_no_secret_and_checks_the_deletion():
    d = (ND / "windows_run.sh").read_text()
    code = "\n".join(l for l in d.splitlines() if not l.lstrip().startswith("#"))
    assert "--nsg-rule NONE" in code and "az group delete" in code and "rg-deletion-proof" in code and "az group exists" in code
    assert "REFUSED: no authenticated Azure CLI session" in code and "az login" not in code and "az ad " not in code and "role assignment" not in code
    assert re.search(r"unset PW", code) and re.search(r"unset KEY SAS SASHEX", code)
    printed = [l for l in code.splitlines() if re.search(r"(echo|printf|log)[^\n]*\$\{?(PW|KEY|SAS)\b", l) and "| xxd" not in l]
    assert not printed, printed
    assert "delete-by" in code and "purpose=release-qualification" in code and "--keep" in code


def test_the_windows_vm_scripts_carry_no_credential_publish_or_signing_step():
    for s in sorted((ND / "windows").glob("*.ps1")):
        code = "\n".join(l for l in s.read_text().splitlines() if not l.lstrip().startswith("#"))
        for pat in (r"signtool", r"Set-AuthenticodeSignature", r"git\s+push", r"git\s+tag", r"Connect-AzAccount", r"az\s+login", r"\.ssh", r"NuGet.*push"):
            assert not re.search(pat, code, re.I), (s.name, pat)
    task = (ND / "windows" / "w_task.ps1").read_text()
    assert "qbuild" in task and "-RunLevel Highest" in task
    b = (ND / "windows" / "w_build.ps1").read_text()
    assert "source identity mismatch" in b and "bundle hash mismatch" in b and b.count("insteadOf") == 2
