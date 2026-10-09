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
