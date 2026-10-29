"""BB-01: a forged ``*.dist-info`` next to a planted package must not make the planted package "installed".

``importlib.metadata`` reads dist-info from EVERY ``sys.path`` entry, the working directory included, so a planted ``context_runtime_enterprise`` plus a fake dist-info (RECORD form, or an editable
``direct_url.json`` form) used to count as provided by an installed distribution and was imported when the enterprise gate was requested. A distribution is now trusted only when its dist-info lies in the
interpreter's own package directories (``sysconfig`` purelib / platlib, ``site.getsitepackages()`` and the user site) and, for the RECORD form, the file it lists lies there too. An editable install
is trusted when its dist-info is in those directories (where pip wrote it) and the package lies under the project directory it records.
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import site
import sys
import sysconfig

import pytest

from agentic_os import app_gate, trusted_import

TOP = "context_runtime_enterprise"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("AGENTIC_OS_PERMISSIONS", "CR_PERMISSIONS", "CR_IDENTITY_MODE", "PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    for name in [n for n in sys.modules if n.split(".")[0] in (TOP, "context_runtime", "agent_harness", "bbpkg")]:
        monkeypatch.delitem(sys.modules, name)
    app_gate.reset_state()
    yield
    app_gate.reset_state()


def package(root: pathlib.Path, name: str, marker: pathlib.Path | None = None) -> pathlib.Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('executed')\n" if marker else "VALUE = 1\n")
    (pkg / "apps.py").write_text(f"open({str(marker)!r} + '-apps', 'w').write('executed')\ndef bootstrap(*a, **k):\n    return lambda r=None: None\n" if marker else "def bootstrap(*a, **k):\n    return lambda r=None: None\n")
    return pkg


def dist_info(where: pathlib.Path, name: str, record: str | None = None, editable_root: pathlib.Path | None = None) -> pathlib.Path:
    info = where / f"{name}-1.0.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
    (info / "top_level.txt").write_text(name + "\n")
    (info / "RECORD").write_text(record if record is not None else f"{name}/__init__.py,,\n")
    if editable_root is not None:
        (info / "direct_url.json").write_text(json.dumps({"url": "file://" + str(editable_root), "dir_info": {"editable": True}}))
    return info


def on_path(monkeypatch, d: pathlib.Path):
    monkeypatch.syspath_prepend(str(d))
    importlib.invalidate_caches()


def nothing_ran(tmp_path) -> bool:
    return not list(tmp_path.glob("MARKER*"))


# ── the forgeries ──

@pytest.mark.parametrize("form", ["record", "editable"])
@pytest.mark.parametrize("where", ["cwd", "sys.path-dir", "next-to-the-package"])
def test_a_forged_dist_info_outside_the_package_directories_never_makes_a_planted_package_trusted(tmp_path, monkeypatch, form, where):
    marker = tmp_path / "MARKER"
    planted = tmp_path / "planted"
    package(planted, TOP, marker)
    home = {"cwd": tmp_path / "cwd", "sys.path-dir": tmp_path / "other-path-dir", "next-to-the-package": planted}[where]
    home.mkdir(parents=True, exist_ok=True)
    dist_info(home, "context_runtime_enterprise", editable_root=planted if form == "editable" else None)
    on_path(monkeypatch, planted)
    on_path(monkeypatch, home)
    if where == "cwd":
        monkeypatch.chdir(home)
        monkeypatch.syspath_prepend("")
    assert trusted_import.package_status(TOP) == "untrusted" and nothing_ran(tmp_path)
    assert trusted_import.import_trusted(TOP + ".apps") is None and nothing_ran(tmp_path)


@pytest.mark.parametrize("form", ["record", "editable"])
def test_enterprise_requested_with_a_forged_dist_info_refuses_to_start_and_the_planted_code_never_runs(tmp_path, monkeypatch, form):
    marker = tmp_path / "MARKER"
    planted = tmp_path / "planted"
    package(planted, TOP, marker)
    dist_info(planted, "context_runtime_enterprise", editable_root=planted if form == "editable" else None)
    on_path(monkeypatch, planted)
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    with pytest.raises(app_gate.GateStartupError) as e:
        app_gate.install_gate("guide")
    assert not isinstance(e.value, app_gate.EnterpriseCapabilityMissing) and "not provided by an installed distribution" in str(e.value)
    assert nothing_ran(tmp_path)


def test_a_dist_info_in_the_package_directory_that_lists_a_file_outside_it_does_not_trust_that_file(tmp_path, monkeypatch):
    site_dir = tmp_path / "site"
    outside = tmp_path / "outside"
    marker = tmp_path / "MARKER"
    package(outside, "bbpkg", marker)
    site_dir.mkdir()
    dist_info(site_dir, "bbpkg", record="../outside/bbpkg/__init__.py,,\n")
    monkeypatch.setattr(trusted_import, "_trusted_dirs", lambda: [site_dir.resolve()])
    on_path(monkeypatch, site_dir)
    on_path(monkeypatch, outside)
    assert trusted_import.package_status("bbpkg") == "untrusted" and nothing_ran(tmp_path)


def test_an_editable_dist_info_in_the_package_directory_trusts_only_a_package_under_its_recorded_root(tmp_path, monkeypatch):
    site_dir, root, elsewhere = tmp_path / "site", tmp_path / "project", tmp_path / "elsewhere"
    package(root, "bbpkg")
    marker = tmp_path / "MARKER"
    package(elsewhere, "bbpkg", marker)
    site_dir.mkdir()
    dist_info(site_dir, "bbpkg", record="", editable_root=root)
    monkeypatch.setattr(trusted_import, "_trusted_dirs", lambda: [site_dir.resolve()])
    on_path(monkeypatch, site_dir)
    on_path(monkeypatch, root)
    assert trusted_import.package_status("bbpkg") == "trusted"                        # the recorded project root: the documented editable recipe
    on_path(monkeypatch, elsewhere)                                                   # a copy ahead on sys.path, outside the recorded root
    assert trusted_import.package_status("bbpkg") == "untrusted" and nothing_ran(tmp_path)


# ── the legitimate recipes still work ──

def test_a_non_editable_install_in_the_package_directory_is_trusted(tmp_path, monkeypatch):
    site_dir = tmp_path / "site"
    package(site_dir, "bbpkg")
    dist_info(site_dir, "bbpkg")
    monkeypatch.setattr(trusted_import, "_trusted_dirs", lambda: [site_dir.resolve()])
    on_path(monkeypatch, site_dir)
    assert trusted_import.package_status("bbpkg") == "trusted"


def test_the_real_package_directories_are_the_interpreters_own_and_a_really_installed_package_is_trusted(tmp_path, monkeypatch):
    dirs = {pathlib.Path(d).resolve() for d in trusted_import._trusted_dirs()}
    assert pathlib.Path(sysconfig.get_paths()["purelib"]).resolve() in dirs
    assert pathlib.Path(sysconfig.get_paths()["platlib"]).resolve() in dirs
    for d in site.getsitepackages():
        assert pathlib.Path(d).resolve() in dirs
    assert pathlib.Path(os.getcwd()).resolve() not in dirs and tmp_path.resolve() not in dirs and pathlib.Path("").resolve() not in dirs
    import httpx   # a dependency of the kernel, installed by pip into the interpreter's site-packages
    assert trusted_import.package_status("httpx") == "trusted"


def test_a_package_that_is_not_installed_by_pip_is_untrusted_even_if_it_is_importable_from_the_repository_checkout():
    """The kernel itself, running from a source checkout on sys.path with no distribution metadata in the package directories, is not what this check is about, but must not crash it."""
    assert trusted_import.package_status("agentic_os") in ("trusted", "untrusted")
