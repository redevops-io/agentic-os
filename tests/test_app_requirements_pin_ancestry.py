"""Transitive pin-ancestry check for the app requirements files under ``apps/*``.

``scripts/check_pin_currency.py`` and the cross-repo pin tests in ``context-runtime``/``agent-harness``
check that each repo's OWN direct pins agree with each other and aren't stale by release count. None of
them walk the chain apps/* -> context-runtime -> agent-harness, so a security fix landed in agent-harness
can be silently absent from every app's install even when context-runtime's own pin "looks current" (it
has moved since the bug this test was added for: all 15 app files pinned context-runtime at a July 2026
commit so old it predated agent-harness even being a separately pinned dependency; the SHA that commit's
own context-runtime pinned for agent-harness was, transitively, two commits behind the M2 authorization-
bypass fix).

This test fails loud, not stale: it names a floor commit per security-relevant fix, resolves each app's
context-runtime pin's OWN agent-harness pin via the GitHub API (no local clone required), and asserts the
result is that floor commit or a descendant of it.
"""
from __future__ import annotations

import base64
import json
import pathlib
import re
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ORG = "redevops-io"

# Floor commits: the earliest agent-harness commit on `main` after which a named security fix is present.
# A pin resolving to this commit or a descendant of it is fine; anything else re-opens the fix it closed.
AGENT_HARNESS_SECURITY_FLOOR = {
    "M2 (ToolRegistry.get().run() authorization bypass, PR #35)": "1259d1b338e80414b32b863bcc97a0db93db8cd2",
}

CONTEXT_RUNTIME_PIN = re.compile(
    r"context-runtime\s*@\s*https://github\.com/redevops-io/context-runtime/archive/"
    r"([0-9a-f]{7,40})\.tar\.gz")
AGENT_HARNESS_PIN = re.compile(
    r"agent-harness\s*@\s*git\+https://github\.com/redevops-io/agent-harness\.git@([0-9a-f]{7,40})")

PIN_FILE_GLOBS = ("requirements*.txt", "requirements*.in", "pyproject.toml")


def _gh(*args: str) -> str:
    p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or f"gh {' '.join(args)} failed")
    return p.stdout


def _gh_available() -> bool:
    try:
        _gh("auth", "status")
        return True
    except Exception:
        return False


def _raw_file(repo: str, path: str, ref: str) -> str:
    """Fetch one file's content at `ref` via the contents API (works for any ref, no clone).
    `ref` MUST be a literal query-string suffix (``?ref=...``), not a `gh api -F` field -- the
    latter is silently dropped for this GET endpoint by at least one `gh` CLI version."""
    out = _gh("api", f"repos/{ORG}/{repo}/contents/{path}?ref={ref}")
    return base64.b64decode(json.loads(out)["content"]).decode("utf-8", "replace")


def _is_descendant_or_equal(repo: str, floor_sha: str, candidate_sha: str) -> bool:
    if floor_sha == candidate_sha or candidate_sha.startswith(floor_sha) or floor_sha.startswith(candidate_sha):
        return True
    out = _gh("api", f"repos/{ORG}/{repo}/compare/{floor_sha}...{candidate_sha}")
    status = json.loads(out)["status"]
    # "ahead": candidate has every floor commit plus more (a true descendant). "identical": same tree.
    return status in ("ahead", "identical")


def _app_context_runtime_pins() -> dict[str, str]:
    """{relative file path: pinned context-runtime sha}, scanning every real apps/* pin file."""
    found: dict[str, str] = {}
    for pattern in PIN_FILE_GLOBS:
        for f in (REPO_ROOT / "apps").glob(f"*/{pattern}"):
            text = f.read_text()
            m = CONTEXT_RUNTIME_PIN.search(text)
            if m:
                found[str(f.relative_to(REPO_ROOT))] = m.group(1)
    return found


@pytest.fixture(scope="module")
def _require_gh():
    if not _gh_available():
        pytest.skip("gh CLI not authenticated in this environment; cannot resolve transitive pins")


def test_every_app_pin_file_found_at_least_one():
    """Sanity check the scanner itself still finds real pin sites (catches a silently-renamed pattern)."""
    pins = _app_context_runtime_pins()
    assert len(pins) >= 10, f"expected at least 10 app files pinning context-runtime, found {len(pins)}: {pins}"


@pytest.mark.parametrize("fix_name,floor_sha", sorted(AGENT_HARNESS_SECURITY_FLOOR.items()))
def test_app_pins_resolve_past_agent_harness_security_floor(_require_gh, fix_name, floor_sha):
    """Every app's context-runtime pin must transitively resolve to an agent-harness commit at or past
    the named security floor. This is the test that would have caught the gap this file was added for:
    at the time it was written, every app pinned a context-runtime commit so old (2026-07-09) that it
    predated agent-harness being a separately pinned dependency at all -- which this test correctly
    treats as a failure (no pin found is not the same as a safe pin)."""
    app_pins = _app_context_runtime_pins()
    assert app_pins, "no app context-runtime pins found at all -- scanner regression, not a clean pass"

    # cache context-runtime's own agent-harness pin per unique context-runtime SHA (apps mostly share one)
    cr_sha_to_ah_sha: dict[str, str | None] = {}
    failures = []
    for path, cr_sha in sorted(app_pins.items()):
        if cr_sha not in cr_sha_to_ah_sha:
            try:
                pyproject = _raw_file("context-runtime", "pyproject.toml", cr_sha)
            except Exception as e:
                cr_sha_to_ah_sha[cr_sha] = None
                failures.append(f"{path}: pinned context-runtime {cr_sha[:12]} -- could not fetch its "
                                 f"pyproject.toml ({e}); treating as unresolved, not passing")
                continue
            m = AGENT_HARNESS_PIN.search(pyproject)
            cr_sha_to_ah_sha[cr_sha] = m.group(1) if m else None
            if not m:
                failures.append(f"{path}: pinned context-runtime {cr_sha[:12]} has no agent-harness pin at "
                                 f"all in its pyproject.toml -- that context-runtime commit predates "
                                 f"agent-harness being a separate pinned dependency; cannot be past any "
                                 f"agent-harness security floor")
                continue

        ah_sha = cr_sha_to_ah_sha[cr_sha]
        if ah_sha is None:
            continue  # already recorded above
        if not _is_descendant_or_equal("agent-harness", floor_sha, ah_sha):
            failures.append(f"{path}: pinned context-runtime {cr_sha[:12]} -> agent-harness {ah_sha[:12]}, "
                             f"which is NOT {floor_sha[:12]} ({fix_name}) or a descendant of it")

    assert not failures, (
        f"{len(failures)} app pin(s) do not transitively clear the '{fix_name}' floor:\n" +
        "\n".join(f"  - {f}" for f in failures))
