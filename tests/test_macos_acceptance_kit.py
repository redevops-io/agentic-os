"""Static and fail-closed checks for the macOS acceptance kit (packaging/acceptance/macos-r0prime).

The kit's rules: bash 3.2, ASCII, no sudo, no keychain (`security`), no GUI (`open`), no rm -r, no change to signatures,
quarantine attributes or system settings, and the docs name no credentials. The functional tests only exercise the
paths that refuse before anything is mounted or started, so they run anywhere bash is available.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent / "packaging" / "acceptance" / "macos-r0prime"
SCRIPTS = sorted(KIT.glob("*.sh"))
DOCS = sorted(KIT.glob("*.md"))
BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"

# command-position patterns the kit must never contain (comments and the documentation are not scanned)
FORBIDDEN = {
    "sudo": r"(^\s*|[;&|(`$]\s*|\s)sudo\s",
    "keychain tool": r"(^\s*|[;&|(`$]\s*)security\s+(find|add|delete|list|unlock|import|set|dump|cms|create|default|show|authorize)",
    "open (GUI)": r"(^\s*|[;&|(`]\s*)open\s+(-|\"|'|\$|/)",
    "recursive rm": r"\brm\s+-[a-zA-Z]*[rR]",
    "xattr write": r"\bxattr\s+(-[a-zA-Z]*[dcw]|--)",
    "signing": r"\bcodesign\s+(-s\b|--sign|--force|--remove-signature)",
    "notarization": r"\bnotarytool\b",
    "gatekeeper change": r"\bspctl\s+(--add|--remove|--master|--enable|--disable|--global)",
    "defaults/launchctl": r"\b(defaults\s+(write|delete)|launchctl\s+(load|bootstrap|unload|bootout|enable|disable))\b",
    "sip": r"\bcsrutil\s+(enable|disable|clear)",
    "rosetta install": r"softwareupdate\b",
}


def _code_lines(path: Path):
    in_doc = False
    for n, line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        s = line.strip()
        if s.startswith("#"):
            continue
        # here-documents that only print text (usage tables, instructions): skip their bodies
        if re.search(r"<<-?\s*'?(TABLE|EOT)'?\s*(\|.*)?$", s):
            in_doc = True
            continue
        if in_doc:
            if s in ("TABLE", "EOT"):
                in_doc = False
            continue
        yield n, line


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_is_ascii_bash_and_parses(script):
    script.read_text(encoding="ascii")  # raises on any non-ASCII byte
    first = script.read_text().splitlines()[0]
    assert first == "#!/bin/bash", first
    r = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_has_no_forbidden_command(script):
    hits = []
    for n, line in _code_lines(script):
        for name, pat in FORBIDDEN.items():
            if re.search(pat, line):
                hits.append(f"{script.name}:{n}: {name}: {line.strip()}")
    assert not hits, "\n".join(hits)


def test_scripts_are_executable_and_complete():
    names = {p.name for p in SCRIPTS}
    assert {"verify_dmg.sh", "post_run_checks.sh", "collect_evidence.sh", "headless_smoke.sh",
            "fallback_throwaway_home.sh", "_lib.sh"} <= names
    for n in names - {"_lib.sh"}:
        assert os.access(KIT / n, os.X_OK), n


def test_no_script_uses_bash4_features():
    bad = re.compile(r"\b(declare|local)\s+-A\b|\bmapfile\b|\breadarray\b|\$\{[A-Za-z_]+(,,|\^\^)\}|&>>|\|&")
    for s in SCRIPTS:
        for n, line in _code_lines(s):
            assert not bad.search(line), f"{s.name}:{n}: {line.strip()}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_docs_are_ascii_and_free_of_banned_claims(doc):
    text = doc.read_text(encoding="ascii")
    for banned in ("open source", "open-source", "tamper-proof", "SOC 2", "CIS benchmark"):
        assert banned.lower() not in text.lower(), (doc.name, banned)
    assert "\t" not in text or doc.name.endswith(".sh")


def test_signing_readiness_names_no_credential():
    text = (KIT / "SIGNING-READINESS.md").read_text()
    assert "Q8A92DM4T7" not in text
    assert not re.search(r"keychain-profile\s+['\"]?[a-z]", text), "a real keychain profile name must not appear"
    assert not re.search(r"\b[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}\b", text), "looks like an app-specific password"
    assert "not executed" in text.lower()
    assert "separate, explicit authorization" in text


def test_intel_plan_makes_no_intel_claim():
    text = (KIT / "INTEL-ROSETTA-TEST-PLAN.md").read_text()
    assert "arm64) only" in text
    assert "No Intel claim is made" in text


def test_checklist_screenshot_names_are_consistent():
    text = (KIT / "MANUAL-CHECKLIST.md").read_text()
    head, _, tail = text.partition("## Screenshot list (names must match)")
    assert tail, "screenshot list missing"
    pat = re.compile(r"\b([A-Z]-\d\d-[A-Za-z0-9<>-]+\.png)")
    used = set(pat.findall(head))
    listed = set(pat.findall(tail))
    assert used and used == listed, (sorted(used - listed), sorted(listed - used))


def _run(*args, cwd=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(cwd or "/tmp"), "LC_ALL": "C"}
    return subprocess.run([BASH, *map(str, args)], capture_output=True, text=True, env=env, cwd=cwd, timeout=60)


@pytest.mark.skipif(sys.platform != "darwin", reason="the kit is macOS only")
def test_verify_dmg_refuses_unlisted_or_mismatching_hash_before_attaching(tmp_path):
    dmg = tmp_path / "Some App.dmg"
    dmg.write_bytes(b"not a real image")
    unlisted = tmp_path / "unlisted.sha256"
    unlisted.write_text("0" * 64 + "  Other.dmg\n")
    r = _run(KIT / "verify_dmg.sh", "--out", tmp_path / "o1", dmg, unlisted, cwd=tmp_path)
    assert r.returncode == 1 and "refusing to attach" in r.stdout and "M-01" not in r.stdout, r.stdout
    wrong = tmp_path / "wrong.sha256"
    wrong.write_text("0" * 64 + "  Some App.dmg\n")
    r = _run(KIT / "verify_dmg.sh", "--out", tmp_path / "o2", dmg, wrong, cwd=tmp_path)
    assert r.returncode == 1 and "MISMATCH" in r.stdout and "M-01" not in r.stdout, r.stdout
    assert dmg.read_bytes() == b"not a real image"


@pytest.mark.skipif(sys.platform != "darwin", reason="the kit is macOS only")
def test_usage_errors_exit_2(tmp_path):
    assert _run(KIT / "verify_dmg.sh", "--expect", "bogus", "a", "b", cwd=tmp_path).returncode == 2
    assert _run(KIT / "verify_dmg.sh", cwd=tmp_path).returncode == 2
    assert _run(KIT / "headless_smoke.sh", tmp_path / "missing.app", cwd=tmp_path).returncode == 2
    assert _run(KIT / "post_run_checks.sh", "--phase", "bogus", cwd=tmp_path).returncode == 2
    assert _run(KIT / "collect_evidence.sh", cwd=tmp_path).returncode == 2
    assert _run(KIT / "fallback_throwaway_home.sh", "command", "--app", "x", "--dir", tmp_path / "d", cwd=tmp_path).returncode == 2


@pytest.mark.skipif(sys.platform != "darwin", reason="the kit is macOS only")
def test_fallback_refuses_the_launch_command_without_acknowledgement(tmp_path):
    (tmp_path / "d" / "home").mkdir(parents=True)
    r = _run(KIT / "fallback_throwaway_home.sh", "command", "--app", tmp_path, "--dir", tmp_path / "d", cwd=tmp_path)
    assert r.returncode == 2 and "refusing" in (r.stderr + r.stdout).lower()
