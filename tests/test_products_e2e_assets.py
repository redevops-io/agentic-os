"""The end-to-end rig under deploy/products-e2e must at least compile and carry no host path or credential."""
from __future__ import annotations

import py_compile
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "products-e2e"


def test_python_compiles_shell_parses_and_nothing_host_specific_is_committed(tmp_path):
    for p in ROOT.glob("*.py"):
        py_compile.compile(str(p), cfile=str(tmp_path / (p.name + "c")), doraise=True)
    for p in ROOT.glob("*.sh"):
        assert subprocess.run(["bash", "-n", str(p)]).returncode == 0
    for p in ROOT.rglob("*"):
        if p.is_file():
            t = p.read_text(encoding="utf-8")
            assert "/Users/" not in t and "qualification" not in t, p
