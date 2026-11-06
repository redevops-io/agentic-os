"""One version identity: the package metadata (pyproject), ``agentic_os.__version__``, the version the control plane and
the Projects API report, and the lock file all agree. The OCI image tag, labels and the app then name the same release."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "0.5.0"


def _pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_package_metadata_and_module_version_agree():
    import agentic_os
    assert _pyproject_version() == agentic_os.__version__ == RELEASE


def test_the_apps_report_the_package_version(monkeypatch, tmp_path):
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setenv("AGENTIC_OS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "1")
    import agentic_os
    from agentic_os import control_plane
    from agentic_os import projects_api
    assert control_plane.app.version == agentic_os.__version__
    assert projects_api.create_app().version == agentic_os.__version__
    outer = projects_api.mounted_app("/projects")
    assert outer.version == agentic_os.__version__
    assert "0.1.0" not in {control_plane.app.version, outer.version}


def test_the_lock_file_names_the_same_version():
    text = (ROOT / "uv.lock").read_text()
    m = re.search(r'\[\[package\]\]\nname = "agentic-os"\nversion = "([^"]+)"', text)
    assert m and m.group(1) == _pyproject_version()
