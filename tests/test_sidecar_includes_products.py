"""The products integration is part of the frozen sidecar: no spec edit is needed because every submodule is collected with
``agentic_os`` and reachable by a STATIC import from ``projects_api``; nothing is excluded; no data file or new dependency.
When a built sidecar is available (``REDEVOPS_FROZEN_SIDECAR``), the real archive listing must contain the modules."""
from __future__ import annotations

import ast
import os
import pkgutil
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "agentic_os"
MODS = ("agentic_os.products", "agentic_os.products.registry", "agentic_os.products.client", "agentic_os.products.hub",
        "agentic_os.products.projections", "agentic_os.products.tools", "agentic_os.products.schemas",
        "agentic_os.products.approvals", "agentic_os.products.gates", "agentic_os.products.chat",
        "agentic_os.products.sentinel", "agentic_os.products.qa", "agentic_os.products.routes",
        "agentic_os.products.principal", "agentic_os.sidekick.capability")


def _spec_kwargs():
    captured: dict = {}

    def collect_submodules(name):
        if name == "agentic_os":
            return ["agentic_os"] + [m.name for m in pkgutil.walk_packages([str(PKG)], "agentic_os.")]
        return [name]

    class Analysis:
        def __init__(self, scripts, **kw):
            captured.update(kw)
            self.pure = []; self.scripts = []; self.binaries = []; self.datas = kw.get("datas", [])

    stub = types.ModuleType("PyInstaller.utils.hooks")
    stub.collect_submodules = collect_submodules
    stub.collect_data_files = lambda name: []
    mods = {"PyInstaller": types.ModuleType("PyInstaller"), "PyInstaller.utils": types.ModuleType("PyInstaller.utils"),
            "PyInstaller.utils.hooks": stub}
    saved = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        code = (ROOT / "packaging" / "redevops-sidecar.spec").read_text(encoding="utf-8")
        exec(compile(code, "redevops-sidecar.spec", "exec"),
             {"Analysis": Analysis, "PYZ": lambda *a, **k: None, "EXE": lambda *a, **k: None, "__name__": "spec"})
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return captured


def test_the_spec_collects_every_products_module_and_excludes_none():
    a = _spec_kwargs()
    for m in MODS:
        assert m in a["hiddenimports"], m
        assert not any(m == e or m.startswith(e + ".") for e in a["excludes"]), m


def test_projects_api_imports_the_products_package_statically():
    tree = ast.parse((PKG / "projects_api.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert "agentic_os.products" in imported


def test_the_package_has_no_dynamic_imports_no_data_files_and_no_new_dependency():
    for p in (PKG / "products").iterdir():
        assert p.suffix == ".py" or p.name == "__pycache__", p
    third_party = set()
    std = set(sys.stdlib_module_names)
    for p in (PKG / "products").glob("*.py"):
        src = p.read_text(encoding="utf-8")
        assert "importlib.import_module" not in src and "__import__(" not in src, p
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Import):
                third_party |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                third_party.add(n.module.split(".")[0])
    assert third_party - std - {"agentic_os", "httpx", "fastapi", "yaml"} == set()      # base dependencies only


@pytest.mark.skipif(not os.environ.get("REDEVOPS_FROZEN_SIDECAR"), reason="needs a built sidecar (REDEVOPS_FROZEN_SIDECAR)")
def test_the_built_sidecar_archive_lists_the_products_modules():
    sys.path.insert(0, str(ROOT / "packaging"))
    from sbom.archive import ExecutableArchive          # noqa: PLC0415
    a = ExecutableArchive(Path(os.environ["REDEVOPS_FROZEN_SIDECAR"]))
    names = set(a.pyz)
    assert [m for m in MODS if m not in names] == []
