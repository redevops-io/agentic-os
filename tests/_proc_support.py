"""Helpers for tests that run a second PROCESS (a lock holder, a concurrent writer) with the repository on its path."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])


def child_env(**extra: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [REPO, env.get("PYTHONPATH", "")]))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def spawn(code: str, *args: str, **env: str) -> subprocess.Popen:
    """``python -c code args...`` with pipes for stdin/stdout, so a test can hand-shake with the child."""
    return subprocess.Popen([sys.executable, "-c", code, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=child_env(**env))
