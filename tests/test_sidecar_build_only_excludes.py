"""setuptools, pkg_resources, _distutils_hack and packaging are build-tool leftovers in the frozen sidecar: prove
nothing shipped imports them, and that the PyInstaller spec leaves them out (register item D-LIC-17).

1. AST: no first-party module (agentic_os, and the sibling packages when installed) imports or dynamically imports them.
2. Spec: executed with a stubbed PyInstaller, they are in `excludes`; the earlier excludes are kept.
3. Frozen (opt-in): REDEVOPS_FROZEN_ARCHIVE_LISTING=<output of `pyi-archive_viewer -l -r redevops-sidecar`> must not name
   them (nor the standard-library modules that only they pulled in), and REDEVOPS_FROZEN_SIDECAR runs the binary
   (bootstrap; the full 63-check smoke test is packaging/candidate/check_app_smoke.py).
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_sidecar_excludes import PKG, ROOT, _run_spec

BUILD_ONLY = ("setuptools", "pkg_resources", "_distutils_hack", "packaging")
# stdlib modules that the archive held only because of the three above (observed: baseline vs excluded archive). `site` is also
# kept out explicitly (SITE_NOT_FROZEN in the spec): agentic_os/trusted_import.py imports it under try/except ImportError (D-NEW-3)
ONLY_VIA_SETUPTOOLS = ("pydoc", "pydoc_data", "rlcompleter", "site", "_sitebuiltins")


def _first_party_roots() -> list[Path]:
    roots = [PKG]
    for name in ("runtime_contracts", "redevops_connectors"):
        spec = importlib.util.find_spec(name)
        if spec and spec.submodule_search_locations:
            roots.append(Path(next(iter(spec.submodule_search_locations))))
    return roots


def _imported(tree: ast.AST) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            fn = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if fn in ("import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                mods.add(node.args[0].value.split(".")[0])
    return mods


def test_no_first_party_module_imports_the_build_only_packages():
    offenders = {}
    for root in _first_party_roots():
        for path in sorted(root.rglob("*.py")):
            hit = _imported(ast.parse(path.read_text(encoding="utf-8"))) & set(BUILD_ONLY + ("distutils",))
            if hit:
                offenders[str(path)] = sorted(hit)
    assert offenders == {}, offenders


def test_spec_excludes_the_build_only_packages_and_keeps_the_earlier_excludes():
    a = _run_spec()
    assert set(BUILD_ONLY) <= set(a["excludes"])
    assert not set(BUILD_ONLY) & set(a["hiddenimports"])
    assert {"tkinter", "redevops_rag", "torch", "knowledge_frontier"} <= set(a["excludes"])
    for needed in ("agentic_os.projects_api", "agentic_os.mission.bootstrap", "uvicorn.loops.auto"):
        assert needed in a["hiddenimports"], needed


def test_duckdb_is_not_excluded_because_a_durable_ledger_backend_needs_it():
    """The unexercised-component review (D-LIC-17) keeps duckdb: MISSION_EVENT_BACKEND=duckdb is the documented
    single-node durable backend, so removing it would change behaviour."""
    a = _run_spec()
    assert "duckdb" not in a["excludes"]
    src = (PKG / "mission" / "event_backends.py").read_text(encoding="utf-8")
    assert re.search(r"backend == [\"']duckdb[\"']", src)


@pytest.mark.skipif(not os.environ.get("REDEVOPS_FROZEN_ARCHIVE_LISTING"),
                    reason="set REDEVOPS_FROZEN_ARCHIVE_LISTING to a `pyi-archive_viewer -l -r` listing of the built sidecar")
def test_frozen_archive_listing_has_none_of_them():
    names = set(re.findall(r"'([^']+)'", Path(os.environ["REDEVOPS_FROZEN_ARCHIVE_LISTING"]).read_text(encoding="utf-8")))
    assert names, "empty listing"
    top = {n.split(".")[0].split("/")[0] for n in names}
    assert not top & set(BUILD_ONLY), sorted(top & set(BUILD_ONLY))
    assert "pyi_rth_setuptools" not in names
    assert not top & set(ONLY_VIA_SETUPTOOLS), sorted(top & set(ONLY_VIA_SETUPTOOLS))
    # what the sidecar needs is still there
    assert {"agentic_os", "uvicorn", "fastapi", "certifi", "cryptography", "duckdb"} <= top


@pytest.mark.skipif(not os.environ.get("REDEVOPS_FROZEN_SIDECAR"),
                    reason="set REDEVOPS_FROZEN_SIDECAR to a built redevops-sidecar")
def test_frozen_sidecar_bootstrap_runs(tmp_path):
    boot = subprocess.run([os.environ["REDEVOPS_FROZEN_SIDECAR"], "bootstrap"], capture_output=True, text=True,
                          env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, timeout=120, cwd=tmp_path)
    assert "Traceback" not in boot.stdout + boot.stderr and "device readiness" in boot.stdout
