"""No workflow in this tree can create a release or publish anything (R0-prime, decision pins-05).

The v0.3.2 native-packaging workflow had a `release` job that published UNSIGNED installers to a GitHub Release
on any `v*` tag. These tests fail on that workflow (feeca065) and pass once the job is gone. They are static:
nothing is run, nothing is contacted.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
ROOT = Path(__file__).resolve().parent.parent

PUBLISH_ACTIONS = ("softprops/action-gh-release", "actions/create-release", "actions/upload-release-asset",
                   "ncipollo/release-action", "marvinpinto/action-automatic-releases", "pypa/gh-action-pypi-publish",
                   "docker/build-push-action", "JamesIves/github-pages-deploy-action", "peaceiris/actions-gh-pages")
PUBLISH_COMMANDS = re.compile(r"gh\s+release|gh\s+api\b|twine\s+upload|npm\s+publish|cargo\s+publish|docker\s+push|"
                              r"git\s+push|git\s+tag|vsce\s+publish|uv\s+publish|pip\s+upload", re.I)
WRITE_PERMS = re.compile(r"^\s*(contents|packages|id-token|deployments|pages|releases|attestations|statuses)\s*:\s*write\b|"
                         r"^\s*permissions\s*:\s*write-all", re.I | re.M)


def violations(text: str) -> list[str]:
    """Everything in a workflow document that could publish, create a release or write with the token."""
    out: list[str] = []
    doc = yaml.safe_load(text)
    jobs = (doc or {}).get("jobs", {}) or {}
    for name, job in jobs.items():
        if re.search(r"release|publish|deploy", name, re.I):
            out.append(f"job named {name!r}")
        for step in (job.get("steps") or []):
            uses = str(step.get("uses", ""))
            if any(uses.startswith(a) for a in PUBLISH_ACTIONS):
                out.append(f"job {name}: publishing action {uses}")
            if PUBLISH_COMMANDS.search(str(step.get("run", ""))):
                out.append(f"job {name}: publishing command in a run step")
        if "if" in job and "refs/tags" in str(job["if"]):
            out.append(f"job {name}: gated on a tag ref")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    if WRITE_PERMS.search(code):
        out.append("a write permission (contents/packages/id-token/... : write, or write-all)")
    if "secrets." in code:
        out.append("uses secrets.*")
    return out


def workflows() -> list[Path]:
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True)
    return [ROOT / f for f in r.stdout.splitlines()
            if re.search(r"(^|/)\.github/workflows/[^/]+\.ya?ml$", f) or re.search(r"(^|/)\.gitea/workflows/", f)
            or re.search(r"(^|/)\.gitlab-ci\.yml$", f)]


# Workflows that main carries and that read a secret WITHOUT being able to publish: a read-only token (permissions: contents: read,
# which `violations` already enforces for every workflow) used to read other repositories' tags. The exception is per file and
# narrow: it only lifts the "uses secrets.*" finding, never the publishing/permission findings (convergence stage 2, decision F7).
READ_ONLY_SECRET_WORKFLOWS = {"pin-currency.yml": "weekly pin-currency report; optional read-only PAT, falls back to github.token"}


def test_the_tree_has_workflows_and_none_can_publish():
    wfs = workflows()
    assert any(p.name == "native-packaging.yml" for p in wfs)
    for p in wfs:
        text = p.read_text(encoding="utf-8")
        found = violations(text)
        if p.name in READ_ONLY_SECRET_WORKFLOWS:
            doc = yaml.safe_load(text)
            assert doc["permissions"] == {"contents": "read"}, p.name       # the exception is only for a read-only token
            found = [v for v in found if v != "uses secrets.*"]
        assert found == [], str(p.relative_to(ROOT))


def test_native_packaging_has_no_release_job_and_a_read_only_token():
    doc = yaml.safe_load((ROOT / ".github/workflows/native-packaging.yml").read_text(encoding="utf-8"))
    assert set(doc["jobs"]) == {"bundle", "acceptance-ubuntu"}
    assert doc[True if True in doc else "on"]["push"]["tags"] == ["v*"]          # build trigger unchanged
    assert doc["permissions"] == {"contents": "read"}
    text = (ROOT / ".github/workflows/native-packaging.yml").read_text(encoding="utf-8")
    assert "action-gh-release" not in "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_no_other_file_carries_a_release_publication_step():
    """Scripts and docs that a workflow might call: nothing in packaging/ publishes."""
    for p in (ROOT / "packaging").glob("*.sh"):
        assert not PUBLISH_COMMANDS.search(p.read_text(encoding="utf-8")), p.name


OLD_RELEASE_JOB = """
name: native-packaging
on:
  push:
    tags: ["v*"]
jobs:
  bundle:
    runs-on: ubuntu-24.04
    steps:
      - run: echo build
  release:
    needs: [bundle]
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-24.04
    permissions:
      contents: write
    steps:
      - name: Publish installers to the GitHub Release
        uses: softprops/action-gh-release@3bb12739c298aeb8a4eeaf626c5b8d85266b0e65  # v2
        with:
          files: release/*
"""


def test_the_detector_flags_the_v032_release_job():
    v = violations(OLD_RELEASE_JOB)
    assert any("job named 'release'" in x for x in v), v
    assert any("softprops/action-gh-release" in x for x in v), v
    assert any("write permission" in x for x in v), v
    assert any("gated on a tag ref" in x for x in v), v


def test_the_frozen_workflow_at_feeca065_is_flagged_when_available():
    r = subprocess.run(["git", "-C", str(ROOT), "show", "feeca065:.github/workflows/native-packaging.yml"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("feeca065 is not in this clone")
    assert violations(r.stdout), "the v0.3.2 workflow must be flagged"
