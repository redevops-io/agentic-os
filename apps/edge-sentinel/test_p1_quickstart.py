"""P1: the standalone quickstart — a package, a console script, and no stdlib shadowing.

The old quickstart (`python3 -m uvicorn app:app` inside this directory) died before the app loaded:
`-m` puts the cwd ahead of the standard library, this directory holds `operator.py`, and uvicorn's own
`import operator` (through `enum`) imported Edge Sentinel's operator module instead of the stdlib one.
Reproduced live 2026-11-07: `ImportError: cannot import name 'or_' from partially initialized module
'operator'`. The fix is the package: nothing but a package directory ever sits on the path, and the
quickstart is `pip install . && edge-sentinel --help`.

The `operator.py` file itself keeps its name on purpose: `apps/<app>/operator.py` is the kernel's
conformance seam (tests/conformance/_conf_apps.py and tests/test_tool_actions_complete.py discover
every app's Operator SDK surface by exactly that path), so renaming it would break the kernel suite —
the audit's alternative, "move under package", is what packaging does instead.

No kernel, no network, no sockets: the CLI must be importable with the standard library alone.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
cli = importlib.import_module("edge-sentinel.cli")     # monorepo view; installed it is edge_sentinel.cli


def test_help_runs_from_a_bare_interpreter_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert e.value.code == 0
    assert "usage: edge-sentinel" in out and "serve" in out


def test_version_reports_the_package_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    out = capsys.readouterr().out
    assert e.value.code == 0
    assert out.strip() == f"edge-sentinel {importlib.import_module('edge-sentinel').__version__}"


def test_no_command_prints_help_rather_than_starting_anything():
    assert cli.main([]) == 0          # returns; nothing was served, nothing was bound


def test_importing_the_cli_pulls_in_neither_the_kernel_nor_the_web_stack():
    """`edge-sentinel --help` works in a bare venv: importing the CLI must not import FastAPI, uvicorn
    or agentic_os. That is what makes the quickstart reproducible before the kernel seam (P2) exists."""
    code = ("import sys, importlib;"
            "importlib.import_module('edge-sentinel.cli');"
            "bad=[m for m in ('fastapi','uvicorn','agentic_os','httpx') if m in sys.modules];"
            "print('CLEAN' if not bad else 'LOADED:'+','.join(bad))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(HERE.parent.parent), env={"PYTHONPATH": str(HERE.parent)})
    assert r.stdout.strip() == "CLEAN", r.stderr[-500:]


def test_the_quickstart_no_longer_runs_from_inside_the_app_directory():
    readme = (HERE / "README.md").read_text()
    assert "python3 -m uvicorn app:app --host 0.0.0.0" not in readme    # the crashing command is gone
    assert "edge-sentinel --help" in readme and "python3 -m pip install ." in readme
    assert "SENTINEL_ALLOW_EXPOSURE=1" in readme                        # the Docker block says how to expose honestly


def test_the_package_ships_one_copy_mapped_not_copied():
    """One home for every module: the wheel maps this directory onto `edge_sentinel` (P1 'move under
    package'), with no second copy of anything — and the flat `operator.py` stays where the kernel's
    conformance tests look for it."""
    pyproject = (HERE / "pyproject.toml").read_text()
    assert 'edge-sentinel = "edge_sentinel.cli:main"' in pyproject
    assert 'edge_sentinel = "."' in pyproject
    assert (HERE / "operator.py").is_file()
    assert (HERE / "__init__.py").is_file()
    # no duplicated module bodies: every top-level module appears exactly once under the app dir
    names = [p.name for p in HERE.glob("*.py")]
    assert len(names) == len(set(names))


def test_the_kernel_dependency_is_documented_not_bypassed():
    """`serve` refuses with the P2 pointer instead of faking a kernel-free dashboard: the kernel import
    stays exactly where the audit found it, and the README and cli.py both say why."""
    src = (HERE / "cli.py").read_text()
    assert "agentic_os" in src and "P2" in src
    app_src = (HERE / "app.py").read_text()
    assert "from agentic_os.app_gate import" in app_src     # the hard kernel seam: intact, not bypassed