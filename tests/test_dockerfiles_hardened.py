"""Static policy for the two release images (``Dockerfile``: control plane, ``Dockerfile.projects``: Projects UI + API).

Nothing is built here (no Docker needed). ``scripts/release/oci_prepare.sh`` builds both images and checks the same properties on the result.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = {"runtime": ROOT / "Dockerfile", "projects": ROOT / "Dockerfile.projects"}


def _logical(text: str) -> list[str]:
    out, cur = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            cur += line[:-1] + " "
            continue
        out.append(cur + line)
        cur = ""
    return out


@pytest.fixture(params=sorted(FILES))
def df(request):
    return request.param, _logical(FILES[request.param].read_text())


def test_base_is_pinned_by_digest(df):
    _, lines = df
    froms = [l for l in lines if l.upper().startswith("FROM ")]
    assert len(froms) == 1 and re.fullmatch(r"FROM python:3\.12-slim@sha256:[0-9a-f]{64}", froms[0]), froms


def test_runs_as_the_unprivileged_user_and_has_a_healthcheck_that_needs_no_extra_tool(df):
    _, lines = df
    assert "USER 10001:10001" in lines
    assert lines.index("USER 10001:10001") > max(i for i, l in enumerate(lines) if l.startswith("RUN "))
    hc = [l for l in lines if l.startswith("HEALTHCHECK")]
    assert len(hc) == 1 and "urllib.request" in hc[0] and "curl" not in hc[0] and "wget" not in hc[0]


def test_copies_an_explicit_file_set_never_the_whole_context(df):
    _, lines = df
    for l in lines:
        m = re.match(r"COPY\s+(?:--\S+\s+)*(.+)", l)
        if m:
            *srcs, _dest = m.group(1).split()
            assert "." not in srcs and "./" not in srcs and "*" not in srcs, l


def test_installs_git_for_the_build_only_and_purges_it_in_the_same_layer(df):
    _, lines = df
    runs = [l for l in lines if l.startswith("RUN ") and "pip install" in l]
    assert len(runs) == 1
    r = runs[0]
    assert "apt-get install" in r and "git" in r and "apt-get purge -y git" in r
    assert "rm -rf /var/lib/apt/lists/*" in r
    assert "--no-cache-dir" in r


def test_the_contract_package_and_the_first_module_that_needs_it_must_import_or_the_build_fails(df):
    _, lines = df
    r = [l for l in lines if l.startswith("RUN ") and "pip install" in l][0]
    assert 'python -c "import runtime_contracts, agentic_os.sources"' in r, "no import verification in the install layer"


def test_pip_and_the_build_tools_are_removed_from_the_final_layers(df):
    _, lines = df
    r = [l for l in lines if l.startswith("RUN ") and "pip install" in l][0]
    assert "pip uninstall -y pip setuptools wheel" in r
    # removed AFTER the last pip use in that layer
    assert r.rindex("pip uninstall") > r.rindex("pip install")
    # nothing after the layer may call pip again
    later = lines[lines.index(next(l for l in lines if l.startswith("RUN ") and "pip install" in l)) + 1:]
    assert not any("pip " in l and not l.startswith("#") for l in later if l.startswith("RUN "))


def test_oci_labels_are_set_and_never_expose_the_repository_or_decide_the_licence(df):
    name, lines = df
    labels = " ".join(l for l in lines if l.startswith("LABEL "))
    for key in ("title", "description", "version", "revision", "vendor"):
        assert f"org.opencontainers.image.{key}=" in labels, key
    assert f'org.opencontainers.image.title="redevops-{name}"' in labels
    for forbidden in ("source", "url", "documentation", "licenses", "authors", "base.name"):
        assert f"org.opencontainers.image.{forbidden}" not in FILES[name].read_text().replace("# ", ""), forbidden
    args = [l for l in lines if l.startswith("ARG ")]
    assert "ARG OCI_VERSION" in args and "ARG OCI_REVISION" in args          # no default: unset means an empty label
    assert "$OCI_VERSION" in labels and "$OCI_REVISION" in labels


def test_reproducible_inputs_are_accepted(df):
    _, lines = df
    assert "ARG SOURCE_DATE_EPOCH" in lines


def test_the_service_user_cannot_write_the_application(df):
    _, lines = df
    text = "\n".join(lines)
    assert "useradd --system --uid 10001" in text and "chown -R" not in text      # /app stays root-owned


def test_runtime_declares_its_writable_paths_projects_its_home():
    rt = "\n".join(_logical(FILES["runtime"].read_text()))
    assert "mkdir -p /data /app/.agentic-os" in rt and "chown app:app /data /app/.agentic-os" in rt
    assert "AGENTIC_OS_SKIP_UI_FETCH=1" in rt                                       # the control plane does not serve the UI bundle
    pj = "\n".join(_logical(FILES["projects"].read_text()))
    assert "--create-home" in pj and "PROJECTS_HOST=0.0.0.0" in pj


def test_every_copy_source_exists_and_survives_dockerignore(df):
    name, lines = df
    pats = [l.strip() for l in (ROOT / ".dockerignore").read_text().splitlines() if l.strip() and not l.startswith("#")]
    for l in lines:
        m = re.match(r"COPY\s+(?:--\S+\s+)*(.+)", l)
        if not m:
            continue
        *srcs, _dest = m.group(1).split()
        for s in srcs:
            p = ROOT / s
            assert p.exists(), s
            files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file() and "__pycache__" not in f.parts]
            assert files, s
            assert not any(fnmatch.fnmatch(s, pat.rstrip("/")) for pat in pats), s


def test_projects_image_copies_what_the_ui_fetch_hook_needs():
    pj = "\n".join(_logical(FILES["projects"].read_text()))
    for need in ("hatch_build.py", "scripts/fetch_projects_ui.py", "packaging/projects_ui.lock.json"):
        assert need in pj, need
