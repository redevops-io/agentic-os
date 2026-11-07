"""`import site` in agentic_os/trusted_import.py must not drag site/pydoc/rlcompleter into the frozen sidecar (D-NEW-3).

0.6.0-rc1: the module imports `site` at top level, PyInstaller followed it, and the frozen archive grew by `site`, `_sitebuiltins`,
`pydoc`, `pydoc_data` and `rlcompleter` (1008 -> 1424 names); tests/test_sidecar_build_only_excludes.py (frozen listing) failed.
The fix keeps trusted_import's rules and excludes `site` from the frozen build: the import is guarded (`site` is None when the
interpreter has no such module, as in the frozen app), and the trusted directories then come from `sysconfig` alone. These tests
prove (unfrozen) that the guard changes nothing about what is trusted; tests/test_frozen_trusted_import.py proves it in the frozen build.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_sidecar_excludes import _run_spec

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "agentic_os" / "trusted_import.py"
PROBE = ROOT / "packaging" / "probes" / "trusted_import_probe.py"


def _clean_env(tmp_path):
    return {k: os.environ[k] for k in ("PATH", "TMPDIR", "LANG", "SYSTEMROOT") if k in os.environ} | {"HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}


def test_site_is_only_imported_inside_a_try_that_catches_import_error():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
                (h.type is not None and "ImportError" in ast.dump(h.type)) or "ModuleNotFoundError" in ast.dump(h.type or ast.Constant(0))
                for h in node.handlers):
            for sub in node.body:
                for n in ast.walk(sub):
                    guarded.add(id(n))
    seen = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "site" for a in node.names) \
                or isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "site":
            seen += 1
            assert id(node) in guarded, f"unguarded import of site at line {node.lineno}"
    assert seen == 1


def test_the_sidecar_spec_excludes_site_and_keeps_the_other_excludes():
    a = _run_spec()
    assert "site" in a["excludes"]
    assert {"tkinter", "redevops_rag", "torch", "knowledge_frontier", "setuptools", "pkg_resources", "_distutils_hack", "packaging"} <= set(a["excludes"])


def _probe(tmp_path, preamble=""):
    code = preamble + f"import runpy, sys; sys.argv=['p', {str(tmp_path / 'w')!r}]; runpy.run_path({str(PROBE)!r}, run_name='__main__')"
    p = subprocess.run([sys.executable, "-c", code], env=_clean_env(tmp_path), capture_output=True, text=True, timeout=120, cwd=tmp_path)
    return p, (json.loads(p.stdout) if p.stdout.strip().startswith("{") else None)


def test_probe_unfrozen_planted_packages_are_untrusted_and_never_run(tmp_path):
    p, r = _probe(tmp_path)
    assert p.returncode == 0, p.stderr
    assert r["frozen"] is False and r["site_loaded"] is True and r["ok"] is True
    assert set(r["planted_status"].values()) == {"untrusted"} and r["planted_code_ran"] == []


def test_probe_with_the_site_module_unavailable_gives_the_same_verdicts(tmp_path):
    """The frozen app has no `site`: simulate that (a None entry in sys.modules makes `import site` raise ImportError)."""
    p, r = _probe(tmp_path, preamble="import sys; sys.modules['site'] = None\n")
    assert p.returncode == 0, p.stderr
    assert r["site_loaded"] is False and r["ok"] is True
    assert set(r["planted_status"].values()) == {"untrusted"} and r["planted_code_ran"] == []


def test_trusted_directories_without_site_are_a_subset_of_those_with_it_and_the_venv_is_still_trusted(tmp_path):
    code = ("import sys, json\nif sys.argv[1] == 'nosite':\n    sys.modules['site'] = None\n"
            "from agentic_os import trusted_import as t\n"
            "print(json.dumps({'dirs': sorted(str(d) for d in t._trusted_dirs()), 'httpx': t.package_status('httpx'), "
            "'agentic_os': t.package_status('agentic_os'), 'absent': t.package_status('definitely_not_installed_pkg')}))\n")
    out = {}
    for mode in ("site", "nosite"):
        p = subprocess.run([sys.executable, "-c", code, mode], env=_clean_env(tmp_path), capture_output=True, text=True, timeout=60, cwd=tmp_path)
        assert p.returncode == 0, p.stderr
        out[mode] = json.loads(p.stdout)
    assert set(out["nosite"]["dirs"]) <= set(out["site"]["dirs"]) and out["nosite"]["dirs"]
    assert out["nosite"]["httpx"] == out["site"]["httpx"] == "trusted"                 # a real installed distribution stays trusted
    assert out["nosite"]["absent"] == out["site"]["absent"] == "absent"
    assert out["nosite"]["agentic_os"] == out["site"]["agentic_os"]


def test_importing_trusted_import_in_a_fresh_interpreter_without_site_does_not_raise(tmp_path):
    p = subprocess.run([sys.executable, "-c", "import sys; sys.modules['site']=None; import agentic_os.trusted_import as t; print(t.site)"],
                       env=_clean_env(tmp_path), capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert p.returncode == 0 and p.stdout.strip() == "None", p.stderr


def test_the_probe_spec_is_the_sidecar_spec_with_only_the_entry_and_the_name_replaced(tmp_path):
    probe_spec = ROOT / "packaging" / "probes" / "trusted-import-probe.spec"
    real = _run_spec()
    # run the probe spec itself: it execs the sidecar spec named by SPECPATH/../redevops-sidecar.spec
    code = probe_spec.read_text(encoding="utf-8")
    import os as _os
    import sys as _sys
    import types as _types
    captured: dict = {}

    class Analysis:
        def __init__(self, scripts, **kw):
            captured.update(kw, scripts=scripts)
            self.pure = []; self.scripts = []; self.binaries = []; self.datas = kw.get("datas", [])
    stub = _types.ModuleType("PyInstaller.utils.hooks")
    stub.collect_submodules = lambda n: [n]
    stub.collect_data_files = lambda n: []
    saved = {k: _sys.modules.get(k) for k in ("PyInstaller", "PyInstaller.utils", "PyInstaller.utils.hooks")}
    _sys.modules.update({"PyInstaller": _types.ModuleType("PyInstaller"), "PyInstaller.utils": _types.ModuleType("PyInstaller.utils"),
                         "PyInstaller.utils.hooks": stub})
    names = []
    try:
        exec(compile(code, str(probe_spec), "exec"), {"Analysis": Analysis, "PYZ": lambda *a, **k: None,
                                                      "EXE": lambda *a, **k: names.append(k.get("name")), "__name__": "spec",
                                                      "SPECPATH": str(probe_spec.parent)})
    finally:
        for k, v in saved.items():
            if v is None:
                _sys.modules.pop(k, None)
            else:
                _sys.modules[k] = v
    assert captured["scripts"] == [str(probe_spec.parent / "trusted_import_probe.py")]
    assert names == ["redevops-trusted-import-probe"]
    assert captured["excludes"] == real["excludes"] and "site" in captured["excludes"]
