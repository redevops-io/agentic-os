"""Guard that every app Dockerfile can actually boot.

The apps' `app.py` use PACKAGE-RELATIVE imports (`from . import core`, …). A Dockerfile that copies only
`app.py` and runs `uvicorn app:app` imports it as a *top-level* module, so the relative import fails at
startup (and `core.py` isn't even copied) — the container can't start. The working shape (edge-sentinel)
copies every module into an importable package dir and runs `uvicorn <pkg>.app:app` with PYTHONPATH=/app.

This is a cheap, Docker-free proxy for a "container boots" smoke: it asserts the Dockerfile uses the package
shape whenever `app.py` needs it, so the boot-breaking regression can't come back unnoticed.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

APPS = Path(__file__).resolve().parents[1] / "apps"
_RELATIVE_IMPORT = re.compile(r"^\s*from\s+\.", re.M)


def _apps_with_relative_import_dashboard():
    for d in sorted(APPS.iterdir()):
        app, df = d / "app.py", d / "Dockerfile"
        if app.exists() and df.exists() and _RELATIVE_IMPORT.search(app.read_text()):
            yield d.name, df


@pytest.mark.parametrize("name,dockerfile",
                         list(_apps_with_relative_import_dashboard()),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_dockerfile_boots_as_a_package(name, dockerfile):
    txt = dockerfile.read_text()
    pkg = name.replace("-", "_")
    # copies ALL modules (not just app.py) into the package dir
    assert re.search(r"^COPY\s+\*\.py\s+\./%s/" % re.escape(pkg), txt, re.M), \
        f"{name}: Dockerfile must COPY *.py into ./{pkg}/ (app.py uses package-relative imports)"
    # runs the package-qualified entrypoint, never the flat one that can't resolve relative imports
    assert f'"{pkg}.app:app"' in txt, f"{name}: entrypoint must be {pkg}.app:app"
    assert '"app:app"' not in txt, f"{name}: flat 'app:app' entrypoint cannot resolve `from . import …`"
    # the package dir must be importable
    assert "PYTHONPATH=/app" in txt, f"{name}: PYTHONPATH=/app is required so {pkg} resolves"


def test_there_are_apps_under_test():
    # guard against the discovery silently finding nothing (e.g. a layout change)
    assert list(_apps_with_relative_import_dashboard()), "no app dashboards discovered"


def _dockerfiles_that_install_the_kernel():
    for d in sorted(APPS.iterdir()):
        df = d / "Dockerfile"
        if df.exists() and "agentic-os" in df.read_text():
            yield d.name, df


@pytest.mark.parametrize("name,dockerfile",
                         list(_dockerfiles_that_install_the_kernel()),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_kernel_pin_is_single_sourced_via_arg(name, dockerfile):
    """The agentic-os kernel version must be pinned in ONE overridable place — the Dockerfile `ARG
    KERNEL_REF` — not duplicated as a stale commit in requirements.txt (which made the images lag the tested
    kernel). Guards both halves: the ARG + ref-based install exist, and requirements.txt no longer pins it."""
    txt = dockerfile.read_text()
    assert re.search(r"^ARG\s+KERNEL_REF=", txt, re.M), f"{name}: Dockerfile must declare ARG KERNEL_REF"
    assert "${KERNEL_REF}" in txt, f"{name}: agentic-os must be installed from ${{KERNEL_REF}}, not a fixed ref"
    req = dockerfile.parent / "requirements.txt"
    if req.exists():
        assert not re.search(r"^agentic-os @", req.read_text(), re.M), \
            f"{name}: requirements.txt must NOT pin agentic-os — the kernel ref lives only in ARG KERNEL_REF"


# ---- kernel pin hardening ------------------------------------------------------------------------------
MIN_KERNEL = (0, 3, 26)  # signed approval tokens + fail-closed verification (and pending_approval)
REPO = Path(__file__).resolve().parents[1]


def _all_dockerfiles():
    return [d / "Dockerfile" for d in sorted(APPS.iterdir()) if (d / "Dockerfile").exists()]


def _kernel_refs():
    out = {}
    for df in _all_dockerfiles():
        m = re.search(r"^ARG\s+KERNEL_REF=(\S+)", df.read_text(), re.M)
        if m:
            out[df.parent.name] = m.group(1)
    return out


def _tuple(ref):
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", ref)
    assert m, f"KERNEL_REF {ref!r} must be a vX.Y.Z release tag (not a branch like main)"
    return tuple(int(x) for x in m.groups())


def test_kernel_ref_is_release_tag_consistent_and_new_enough():
    refs = _kernel_refs()
    assert "_suite-server" in refs and len(refs) >= 10
    assert len(set(refs.values())) == 1, f"app images pin different kernels: {refs}"
    assert _tuple(next(iter(refs.values()))) >= MIN_KERNEL


def test_requirements_txt_files_do_not_pin_old_kernel():
    for req in APPS.glob("*/requirements.txt"):
        for m in re.finditer(r"agentic-os\s*@\s*\S*?/(v\d+\.\d+\.\d+)\.tar\.gz", req.read_text()):
            assert _tuple(m.group(1)) >= MIN_KERNEL, f"{req}: stale kernel pin {m.group(1)}"


def test_pinned_kernel_exports_every_operator_sdk_symbol_apps_import():
    import ast
    sdk = ast.parse((REPO / "agentic_os/mission/operator_sdk.py").read_text())
    defined = set()
    for n in sdk.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            defined.add(n.name)
        elif isinstance(n, ast.Assign):
            defined |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            defined.add(n.target.id)
    imported = set()
    for f in APPS.glob("*/*.py"):
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.ImportFrom) and n.module == "agentic_os.mission.operator_sdk":
                imported |= {a.name for a in n.names}
    assert "pending_approval" in imported
    assert imported <= defined, f"apps import symbols missing from operator_sdk: {imported - defined}"


# ---- COPY sources must exist on a clean checkout -------------------------------------------------------
def _copy_sources(df):
    """(source paths relative to the build context) for each COPY; context is the app dir, except
    _suite-server whose context is the repo root."""
    ctx = REPO if df.parent.name == "_suite-server" else df.parent
    txt = df.read_text().replace("\\\n", " ")
    for line in txt.splitlines():
        m = re.match(r"\s*COPY\s+(?!--from)(.*)", line)
        if not m:
            continue
        parts = [p for p in m.group(1).split() if not p.startswith("--")]
        for src in parts[:-1]:
            yield ctx, src


@pytest.mark.parametrize("df", _all_dockerfiles(), ids=lambda p: p.parent.name)
def test_every_copy_source_exists_in_repo(df):
    import glob
    for ctx, src in _copy_sources(df):
        assert glob.glob(str(ctx / src)), f"{df.parent.name}: COPY source {src!r} missing from a clean checkout"


def test_every_app_has_identical_license():
    ref = (APPS / "billing/LICENSE").read_bytes()
    for d in sorted(APPS.iterdir()):
        if (d / "Dockerfile").exists():
            assert (d / "LICENSE").read_bytes() == ref, f"{d.name}: LICENSE missing or differs"
