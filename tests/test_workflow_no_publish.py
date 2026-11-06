"""No workflow in this tree can publish a container image or write to a registry, except ONE: `.github/workflows/oci-promote.yml`.

That workflow only PROMOTES an image that was staged and qualified beforehand (it has no build step), runs from `workflow_dispatch` on
`refs/heads/main` in the protected environment `release-promotion`, and has exactly the permissions it needs. These tests are static:
nothing is run and nothing is contacted. Any other workflow that asks for `packages: write`, `id-token: write` or `attestations: write`,
builds and pushes an image, or signs or copies an image fails here; so does a modified copy of the promotion workflow (more permissions,
another trigger, a build step, an unpinned action, a different file name).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
ROOT = Path(__file__).resolve().parent.parent
PROMOTION = ".github/workflows/oci-promote.yml"

IMAGE_PUBLISH_ACTIONS = ("docker/build-push-action", "docker/bake-action", "redhat-actions/push-to-registry", "actions/attest-build-provenance",
                         "actions/attest-sbom", "actions/attest@", "sigstore/gh-action-sigstore-python")
IMAGE_PUBLISH_COMMANDS = re.compile(
    r"docker\s+(?:image\s+)?push\b|docker\s+buildx\s+imagetools\s+create|docker\s+buildx\s+build\b[^\n]*--push|--output[ =][^\n]*push=true|"
    r"\bcosign\s+(?:sign|attest|copy|save|load)\b|\boras\s+(?:push|cp|copy|tag)\b|\bcrane\s+(?:push|copy|cp|tag)\b|\bskopeo\s+(?:copy|sync)\b", re.I)
REGISTRY_WRITE_PERMS = re.compile(r"^\s*(packages|id-token|attestations)\s*:\s*write\b|^\s*permissions\s*:\s*write-all", re.I | re.M)
SHA_PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _code(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def violations(text: str) -> list[str]:
    """Everything in a workflow document that could publish an image or write to a registry."""
    out: list[str] = []
    doc = yaml.safe_load(text) or {}
    for name, job in (doc.get("jobs", {}) or {}).items():
        for step in (job.get("steps") or []):
            uses = str(step.get("uses", ""))
            if any(uses.startswith(a) for a in IMAGE_PUBLISH_ACTIONS):
                out.append(f"job {name}: image publishing action {uses}")
            if IMAGE_PUBLISH_COMMANDS.search(str(step.get("run", ""))):
                out.append(f"job {name}: image publishing command in a run step")
    if REGISTRY_WRITE_PERMS.search(_code(text)):
        out.append("a registry write permission (packages/id-token/attestations: write, or write-all)")
    return out


def promotion_violations(text: str) -> list[str]:
    """The one allowed workflow, checked structurally. [] means it is exactly the reviewed shape."""
    out: list[str] = []
    doc = yaml.safe_load(text) or {}
    on = doc.get(True, doc.get("on"))
    if not (isinstance(on, dict) and set(on) == {"workflow_dispatch"}):
        out.append(f"trigger must be workflow_dispatch only, got {on!r}")
    if doc.get("permissions") != {"contents": "read"}:
        out.append(f"top-level permissions must be exactly contents: read, got {doc.get('permissions')!r}")
    jobs = doc.get("jobs") or {}
    if list(jobs) != ["promote"]:
        out.append(f"exactly one job named promote, got {list(jobs)}")
        return out
    job = jobs["promote"]
    if job.get("permissions") != {"contents": "read", "packages": "write", "id-token": "write"}:
        out.append(f"job permissions must be exactly contents: read, packages: write, id-token: write, got {job.get('permissions')!r}")
    if job.get("environment") != "release-promotion":
        out.append("the job must run in the protected environment release-promotion")
    if "if" in job:
        out.append("no job-level condition")
    code = _code(text)
    steps = job.get("steps") or []
    for s in steps:
        uses = str(s.get("uses", ""))
        if uses and not SHA_PINNED.match(uses):
            out.append(f"action not pinned by a 40-hex commit SHA: {uses}")
        if any(uses.startswith(a) for a in IMAGE_PUBLISH_ACTIONS):
            out.append(f"build/publish action {uses}")
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    for pat, why in ((r"docker\s+(?:image\s+)?push\b", "docker push"), (r"docker\s+(?:buildx\s+)?build\b", "a build step"),
                     (r"\bgit\s+(?:push|tag)\b", "git push/tag"), (r"\bgh\s+(?:release|api)\b", "gh release/api"),
                     (r"\boras\s+(?:push|cp|copy)\b", "oras"), (r"\bcosign\s+(?:copy|save|load)\b", "cosign copy")):
        if re.search(pat, runs):
            out.append(f"forbidden in the promotion workflow: {why}")
    if "buildx imagetools create" not in runs:
        out.append("the copy step (docker buildx imagetools create) is missing")
    if "secrets." in code and set(re.findall(r"secrets\.([A-Za-z_]+)", code)) != {"GITHUB_TOKEN"}:
        out.append("secrets other than GITHUB_TOKEN")
    first = str((steps[0] or {}).get("run", "")) if steps else ""
    if not re.search(r'\[ "\$GITHUB_REF" = refs/heads/main \]', first) or not re.search(r'\[ "\$GITHUB_EVENT_NAME" = workflow_dispatch \]', first):
        out.append("the first step must refuse anything but workflow_dispatch on refs/heads/main")
    ident = re.findall(r"^\s*IDENTITY:\s*(\S+)", code, re.M)
    if ident != ["https://github.com/redevops-io/agentic-os/.github/workflows/oci-promote.yml@refs/heads/main"]:
        out.append(f"the verification identity must be the exact workflow path at refs/heads/main, got {ident}")
    if re.search(r"certificate-identity-regexp", code):
        out.append("verification must use an exact identity, not a pattern")
    return out


def workflows() -> list[Path]:
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True)
    return [ROOT / f for f in r.stdout.splitlines()
            if re.search(r"(^|/)\.github/workflows/[^/]+\.ya?ml$", f) or re.search(r"(^|/)\.gitea/workflows/", f)
            or re.search(r"(^|/)\.gitlab-ci\.yml$", f)]


def test_no_workflow_but_the_promotion_one_can_publish_an_image():
    wfs = workflows()
    assert wfs
    for p in wfs:
        rel = str(p.relative_to(ROOT))
        if rel == PROMOTION:
            continue
        assert violations(p.read_text(encoding="utf-8")) == [], rel


def test_the_promotion_workflow_exists_and_is_exactly_the_reviewed_shape():
    text = (ROOT / PROMOTION).read_text(encoding="utf-8")
    assert promotion_violations(text) == []
    assert PROMOTION in [str(p.relative_to(ROOT)) for p in workflows()]


def test_the_promotion_workflow_is_the_only_file_that_needs_the_registry_permissions():
    with_perms = [str(p.relative_to(ROOT)) for p in workflows() if REGISTRY_WRITE_PERMS.search(_code(p.read_text(encoding="utf-8")))]
    assert with_perms == [PROMOTION]


# ───────────────────────── the detectors must flag the things they are for ─────────────────────────
GOOD = None


def _good() -> str:
    return (ROOT / PROMOTION).read_text(encoding="utf-8")


def _mut(old: str, new: str) -> str:
    t = _good()
    assert old in t, old
    return t.replace(old, new, 1)


@pytest.mark.parametrize("label,text", [
    ("pull_request trigger", lambda: _mut("workflow_dispatch:\n", "pull_request:\n  workflow_dispatch:\n")),
    ("push trigger", lambda: _mut("workflow_dispatch:\n", "push:\n    branches: [main]\n  workflow_dispatch:\n")),
    ("schedule trigger", lambda: _mut("workflow_dispatch:\n", "schedule:\n    - cron: '0 3 * * *'\n  workflow_dispatch:\n")),
    ("extra permission", lambda: _mut("      id-token: write\n", "      id-token: write\n      attestations: write\n")),
    ("write-all", lambda: _mut("      contents: read\n      packages: write", "      contents: write\n      packages: write")),
    ("no environment", lambda: _mut("    environment: release-promotion\n", "")),
    ("other environment", lambda: _mut("environment: release-promotion", "environment: staging")),
    ("build-push action", lambda: _mut("      - name: Validate inputs", "      - uses: docker/build-push-action@" + "a" * 40 + "\n        with: {push: true}\n      - name: Validate inputs")),
    ("unpinned action", lambda: _mut("      - name: Validate inputs", "      - uses: actions/checkout@v4\n      - name: Validate inputs")),
    ("docker build", lambda: _mut("docker buildx imagetools create", "docker build -t x . && docker buildx imagetools create")),
    ("docker push", lambda: _mut("docker buildx imagetools create", "docker push x && docker buildx imagetools create")),
    ("identity regexp", lambda: _mut("cosign verify --certificate-identity \"$IDENTITY\"", "cosign verify --certificate-identity-regexp '^https://github.com/redevops-io/' ")),
    ("identity on a tag ref", lambda: _mut("oci-promote.yml@refs/heads/main\n", "oci-promote.yml@refs/tags/v0.5.0\n")),
    ("wildcard identity", lambda: _mut("oci-promote.yml@refs/heads/main\n", "oci-promote.yml@refs/heads/.*\n")),
    ("second job", lambda: _good() + "  extra:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: echo hi\n"),
    ("other secret", lambda: _mut("TOKEN: ${{ secrets.GITHUB_TOKEN }}", "TOKEN: ${{ secrets.REGISTRY_PASSWORD }}")),
    ("no branch guard", lambda: _mut("refs/heads/main ] ||", "refs/heads/dev ] ||")),
])
def test_the_promotion_detector_flags(label, text):
    assert promotion_violations(text()), label


def test_the_general_detector_flags_a_build_and_push_workflow():
    bad = ("name: x\non: [push]\njobs:\n  b:\n    runs-on: ubuntu-24.04\n    permissions:\n      packages: write\n    steps:\n"
           "      - uses: docker/build-push-action@" + "b" * 40 + "\n        with: {push: true}\n")
    v = violations(bad)
    assert any("build-push" in x for x in v) and any("registry write permission" in x for x in v), v
    assert violations("name: x\non: [push]\njobs:\n  b:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: cosign sign x\n")
    assert violations("name: x\non: [push]\njobs:\n  b:\n    runs-on: ubuntu-24.04\n    permissions:\n      id-token: write\n    steps:\n      - run: echo\n")
    assert violations("name: x\non: [push]\njobs:\n  b:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: docker buildx imagetools create -t a b\n")


def test_a_copy_of_the_promotion_workflow_under_another_name_is_not_allowed(tmp_path):
    """The allowance is by path: the same text anywhere else is a workflow with registry write permissions."""
    assert violations(_good()), "the promotion workflow's own content must be flagged by the general detector (it is allowed by path only)"


def test_ordinary_ci_workflows_are_untouched_by_the_detector():
    ok = "name: ci\non: [pull_request]\npermissions:\n  contents: read\njobs:\n  t:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: pytest\n"
    assert violations(ok) == []
