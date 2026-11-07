"""Is the ``agentic_os`` that a subprocess of this session would import the one in THIS repository? (see tests/test_env_guard.py)

Used by tests/conftest.py at session start. Stdlib only; it must not import ``agentic_os`` itself, because the question is where an import WOULD resolve.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_PROBE = "import importlib.util, os; s = importlib.util.find_spec('agentic_os'); print(os.path.realpath(s.origin) if s and s.origin else '')"


def ambient_location(python: str | None = None) -> str | None:
    """Where ``agentic_os`` resolves for ``python -c`` started in a fresh, empty cwd with this process's environment: what ``subprocess.run([sys.executable, ...], cwd=tmp)``
    in a test sees. (In-process, pytest has already put the repository root on sys.path, which would hide a wrong install.)"""
    with tempfile.TemporaryDirectory(prefix="agentic-os-guard-") as cwd:
        try:
            r = subprocess.run([python or sys.executable, "-c", _PROBE], cwd=cwd, capture_output=True, text=True, timeout=120, env=dict(os.environ))
        except (OSError, subprocess.TimeoutExpired):
            return None
    return r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else None


def mismatch(root: Path, found: str | None) -> str | None:
    """None when ``found`` (a path to agentic_os/__init__.py) lies inside ``root``; otherwise the message that names both paths and the fix."""
    root = Path(root).resolve()
    if found:
        try:
            Path(found).resolve().relative_to(root)
            return None
        except ValueError:
            pass
    return (f"agentic_os resolves to {found or '<nothing: it is not importable>'} for a subprocess started from another directory, but these tests belong to {root}. "
            f"A subprocess started by a test would import that other code, and the tests would fail (or pass) for the wrong reason. "
            f"Fix: create an environment bound to this worktree with scripts/testenv/make_env.sh --worktree {root} --env-dir <a directory outside the repository> "
            f"and run the suite with scripts/testenv/run_tests.sh. Do not hide this with PYTHONPATH: the install must be the right one.")


def check_session(root: Path) -> None:
    """Raise AssertionError (never continue) when this session is bound to another worktree: in-process (a module already imported from elsewhere) or ambient (subprocess)."""
    loaded = sys.modules.get("agentic_os")
    if loaded is not None and getattr(loaded, "__file__", None):
        msg = mismatch(root, loaded.__file__)
        if msg:
            raise AssertionError(f"[in-process] {msg}")
    msg = mismatch(root, ambient_location())
    if msg:
        raise AssertionError(msg)
