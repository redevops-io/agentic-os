"""The commits this repository installs from, checked together instead of sixteen times apart.

Pins drift quietly: ``permissions-demo-compat.yml`` — the job whose whole job description is proving the
permissions gate still works — installed a harness commit that predated the authorization work it was
gating, and nobody noticed because each of the sixteen app requirement files carries its own copy of a
commit SHA and no test ever looked at two of them at once.

What a repository CAN check about its dependencies offline is that they are pinned at all, that they are
pinned to immutable objects rather than moving names, and that the copies agree. That is what runs here.
What it cannot check offline — whether the pinned commit is the one the dependency now requires — is the
line in the pull request that a reviewer has to read, and the pin matrix in the PR body is what makes it
readable rather than guessable.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
APPS = ROOT / "apps"
WORKFLOWS = ROOT / ".github" / "workflows"

_CR_PIN = re.compile(r"context-runtime @ https://github\.com/redevops-io/context-runtime/archive/"
                     r"(?P<sha>[0-9a-f]+)\.tar\.gz")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _context_runtime_pins() -> dict[str, list[str]]:
    """``{pinned commit: [files that pin it]}`` across every app requirement file and pyproject."""
    pins: dict[str, list[str]] = {}
    for path in sorted(APPS.rglob("*")):
        if path.suffix not in {".txt", ".toml", ".in"} or not path.is_file():
            continue
        for match in _CR_PIN.finditer(path.read_text(encoding="utf-8")):
            pins.setdefault(match.group("sha"), []).append(str(path.relative_to(ROOT)))
    return pins


def test_the_scan_finds_the_pinned_dependency_at_all():
    """A guard that matches nothing is the same as no guard."""
    pins = _context_runtime_pins()
    assert sum(len(files) for files in pins.values()) >= 10, f"only {pins} — did the pin format change?"


def test_every_app_installs_the_same_context_runtime_commit():
    """Sixteen copies of a commit SHA, and one answer. Half-moved pins are how a fleet ends up running two
    versions of the same authorization code and blaming the app that looks wrong."""
    pins = _context_runtime_pins()
    assert len(pins) == 1, {sha[:12]: files for sha, files in pins.items()}


def test_dependencies_are_pinned_to_commits_not_to_names():
    """A branch name is not a pin: the gate would pass today on a commit that does not exist tomorrow."""
    for sha, files in _context_runtime_pins().items():
        assert _FULL_SHA.match(sha), f"{sha!r} in {files} is not a full commit SHA"


def test_the_permissions_gate_installs_a_pinned_agent_harness():
    """The job that runs ``tests/test_permissions_*.py`` must not be able to install a moving dependency:
    the harness decides what an authorizer's answer MEANS, so a floating harness is a floating gate."""
    workflow = (WORKFLOWS / "permissions-demo-compat.yml").read_text(encoding="utf-8")
    pinned = re.findall(r"agent-harness @ git\+https://github\.com/redevops-io/agent-harness\.git@([0-9a-zA-Z._-]+)",
                        workflow)
    assert len(pinned) == 1, f"expected exactly one harness pin in the permissions gate, found {pinned}"
    assert _FULL_SHA.match(pinned[0]), f"the permissions gate installs agent-harness at {pinned[0]!r}, not a commit"


def test_no_dependency_pin_points_at_a_moving_branch():
    """Same rule, everywhere: every ``git+https://…@ref`` in the install manifests must name a commit."""
    offenders = []
    for path in sorted(list(APPS.rglob("requirements*.txt")) + list(APPS.rglob("requirements*.in"))
                       + list(APPS.rglob("pyproject.toml"))):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or "git+https://" not in line:
                continue
            ref = line.rsplit("@", 1)[-1].strip().strip('"')
            if not _FULL_SHA.match(ref):
                offenders.append(f"{path.relative_to(ROOT)}: {line.strip()[:90]}")
    assert not offenders, f"install manifests pinned to something that can move: {offenders}"