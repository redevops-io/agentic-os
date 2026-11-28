"""No workflow in this tree can create a release or publish anything, except the few named ones below (R0-prime,
decision pins-05; narrowly reversed 2026-11-19, owner-approved, for exactly the workflows in
PUBLISH_ALLOWED_WORKFLOWS and OIDC_SIGNING_WORKFLOWS - everything else is still refused in full).

The v0.3.2 native-packaging workflow had a `release` job that published UNSIGNED installers to a GitHub Release
on any `v*` tag. These tests fail on that workflow (feeca065) and pass once the job is gone. They are static:
nothing is run, nothing is contacted.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import yaml
ROOT = Path(__file__).resolve().parent.parent

PUBLISH_ACTIONS = ("softprops/action-gh-release", "actions/create-release", "actions/upload-release-asset",
                   "ncipollo/release-action", "marvinpinto/action-automatic-releases", "pypa/gh-action-pypi-publish",
                   "docker/build-push-action", "JamesIves/github-pages-deploy-action", "peaceiris/actions-gh-pages")
PUBLISH_COMMANDS = re.compile(r"gh\s+release(?!\s+download\b)|gh\s+api\b|twine\s+upload|npm\s+publish|cargo\s+publish|docker\s+push|"
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

# Workflows that main carries and that REPORT or READ, and can publish nothing. Each exception is per file and checked
# below: the token stays read-only (`violations` still reports any write permission), the only secret named is the one
# listed, and every `gh api` call is a plain GET.
REPORT_SECRET = "QA_DASHBOARD_TOKEN"                  # sends test results to the QA dashboard; no repository right
REPORTING_WORKFLOWS = {"tests.yml": "reports the test results", "report-qa-jobs.yml": "reports a finished run's job results"}
READ_ONLY_API_WORKFLOWS = {"report-qa-jobs.yml": "reads a finished run's job list",
                           "published-artifacts-acceptance.yml": "reads a published release's record to check it"}
_API_WRITES = re.compile(r"(^|\s)(-X|--method|-f|-F|--field|--raw-field|--input)([\s=]|$)")


def read_only_exceptions(name: str, text: str, found: list[str]) -> list[str]:
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    if name in REPORTING_WORKFLOWS and "uses secrets.*" in found:
        named = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", code))
        assert named == {REPORT_SECRET}, f"{name} may name only {REPORT_SECRET}, not {sorted(named)}"
        found = [v for v in found if v != "uses secrets.*"]
    if name in READ_ONLY_API_WORKFLOWS:
        calls = re.findall(r"gh\s+api\b[^\n]*", code)
        assert calls and not [c for c in calls if _API_WRITES.search(c.split("--jq")[0])], f"{name}: every gh api call must only read"
        without = re.sub(r"gh\s+api\b", "gh read", text)
        found = [v for v in found if not (v.endswith("publishing command in a run step") and not any(
            x.endswith("publishing command in a run step") for x in violations(without)))]
    return found


# id-token: write used ONLY to authenticate to a real signing service via OIDC (azure/login) - never to publish a
# release, push an image or write any secret. The exception lifts just the one "write permission" finding; a
# publish action, a release-shaped job name, or secrets.* usage in the same file would still fail this test.
OIDC_SIGNING_WORKFLOWS = {
    "windows-signing-r0prime.yml": "Azure Trusted Signing OIDC login for the two real-signing jobs; no stored secret, no publish action",
    "windows-signing-r0prime-arm64.yml": "same as windows-signing-r0prime.yml, second architecture (ARM64) of the same release; no stored secret, no publish action",
}

# Owner-approved, narrow reversal of pins-05 (2026-11-19): these named workflows MAY publish - each one is
# workflow_dispatch-only, requires a typed confirmation input (a person acts, not a push/PR/schedule), and
# publishes only already-signed, already-gate-checked artifacts. This is still "no workflow may publish by
# accident": every OTHER file in the tree is still held to the full, zero-exception check below.
PUBLISH_ALLOWED_WORKFLOWS = {
    "oci-edge-sentinel.yml": "signed GHCR publish of the Edge Sentinel standalone image",
    "oci-redevops-ts-bundle.yml": "signed GHCR publish of the combined bundle (Projects+Sidekick, redevops-qa, Edge Sentinel) OCI artifact",
    "oci-redevops-ts.yml": "signed GHCR publish of the single-pull bundle, from main only; container images and a compose artifact, never installers",
    "oci-release.yml": "signed GHCR publish of the runtime and Projects images, built from a release tag contained in main; never installers",
    "oci-redevops-projects.yml": "signed GHCR publish of the Projects UI standalone image (redevops-projects), gated on the UI source actually being published",
    "release-publish.yml": "GitHub Release of the signed R0-prime Windows installers, gated on the distribution gate passing",
    "snap-railsapps.yml": "build and Snap Store edge-channel publish of railsapps, gated on the distribution gate passing and a confirmation phrase naming the known SNAP-D1 defect",
    "snap-railsapps-metadata.yml": "Snap Store listing text (summary/description/icon) push for railsapps via `snapcraft upload-metadata`, no new revision published",
}


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
        if p.name in OIDC_SIGNING_WORKFLOWS:
            found = [v for v in found if v != "a write permission (contents/packages/id-token/... : write, or write-all)"]
        found = read_only_exceptions(p.name, text, found)
        if p.name in PUBLISH_ALLOWED_WORKFLOWS:
            doc = yaml.safe_load(text)
            triggers = doc.get(True, doc.get("on", {})) or {}
            assert set(triggers) == {"workflow_dispatch"}, \
                f"{p.name}: a publish-allowed workflow must be workflow_dispatch-only, got {sorted(triggers)}"
            assert "confirm" in (triggers["workflow_dispatch"].get("inputs") or {}), \
                f"{p.name}: a publish-allowed workflow must require a typed confirmation input"
            found = []
        assert found == [], str(p.relative_to(ROOT))


def test_only_the_signed_desktop_publisher_creates_a_github_release():
    """Whatever else may publish an image or a store listing, installers reach a release through one workflow."""
    makers = []
    for p in workflows():
        code = "\n".join(l for l in p.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
        if re.search(r"gh\s+release\s+(create|upload)", code) or any(a in code for a in PUBLISH_ACTIONS[:5]):
            makers.append(p.name)
    assert makers == ["release-publish.yml"]


def test_the_bundle_publisher_runs_from_main_only_signs_by_digest_and_overwrites_nothing():
    text = (ROOT / ".github/workflows/oci-redevops-ts.yml").read_text(encoding="utf-8")
    assert '[ "$GITHUB_REF" = "refs/heads/main" ]' in text and '[ "$CONFIRM" = "PUBLISH $TAG" ]' in text
    assert re.search(r'cosign sign --yes --recursive "ghcr\.io/redevops-io/redevops-projects@\$\{\{ steps\.build\.outputs\.digest \}\}"', text)
    assert "Refuse to overwrite an existing" in text and "contents: write" not in text


def test_the_release_image_publisher_builds_only_a_tag_contained_in_main_signs_it_and_overwrites_nothing():
    text = (ROOT / ".github/workflows/oci-release.yml").read_text(encoding="utf-8")
    assert '[ "$GITHUB_REF" = "refs/tags/$REF" ]' in text and '[ "$CONFIRM" = "PUBLISH $REF" ]' in text
    assert 'git merge-base --is-ancestor "$COMMIT" origin/main' in text and "Refuse to overwrite an existing tag" in text
    assert 'cosign sign --yes --recursive "ghcr.io/redevops-io/${NAME}@${{ steps.build.outputs.digest }}"' in text
    assert "contents: write" not in text


@pytest.mark.parametrize("name,added", [
    ("report-qa-jobs.yml", 'run: gh api -X POST "repos/x/y/releases" -f tag_name=v1'),
    ("report-qa-jobs.yml", "token: ${{ secrets.RELEASE_TOKEN }}"),
    ("published-artifacts-acceptance.yml", "run: gh release create v1 dist/*"),
    ("tests.yml", "token: ${{ secrets.SNAPCRAFT_STORE_CREDENTIALS }}"),
])
def test_a_read_only_exception_does_not_cover_anything_that_writes(name, added):
    """Each exception, tried against the change it must still refuse."""
    text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    doc["jobs"]["zz_added"] = {"runs-on": "ubuntu-latest", "steps": [
        {"run": added[5:]} if added.startswith("run: ") else {"uses": "actions/checkout@v4", "with": {"token": added[7:]}}]}
    changed = yaml.safe_dump(doc, sort_keys=False)
    try:
        found = read_only_exceptions(name, changed, violations(changed))
    except AssertionError:
        return                                                          # the exception itself refused it
    assert found, f"{name} would have been let through with: {added}"


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
