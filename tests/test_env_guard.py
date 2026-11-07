"""The test session must run against THIS worktree's code, and must say so loudly when it does not.

A venv with ``agentic_os`` installed (editable or copied) from ANOTHER worktree makes every subprocess a test starts (``[sys.executable, "-m", ...]`` from a temp cwd)
import the other worktree's code: the in-process tests look fine (pytest puts the repository root on sys.path) and the subprocess tests fail for no visible reason.
tests/conftest.py therefore resolves ``agentic_os`` the way such a subprocess does and refuses to start when that is not inside this repository (tests/_env_guard.py).
scripts/testenv/make_env.sh creates an environment bound to one worktree and verifies the binding; run_tests.sh refuses an environment made for another one.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _env_guard

ROOT = Path(__file__).resolve().parent.parent
TESTENV = ROOT / "scripts" / "testenv"
CLEAN = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")}      # no PYTHONPATH, no credentials


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "-C", str(cwd), *args],
                       capture_output=True, text=True, env={**CLEAN, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


# --- the check ------------------------------------------------------------------------------------------------------------------------------------
def test_a_location_inside_the_repository_passes():
    assert _env_guard.mismatch(ROOT, str(ROOT / "agentic_os" / "__init__.py")) is None


@pytest.mark.parametrize("found", ["/some/other/worktree/agentic_os/__init__.py", str(ROOT.parent / (ROOT.name + "-sibling") / "agentic_os" / "__init__.py"), None, ""])
def test_a_location_outside_the_repository_or_none_is_reported_with_both_paths_and_the_fix(found):
    msg = _env_guard.mismatch(ROOT, found)
    assert msg and str(ROOT) in msg
    if found:
        assert found in msg
    assert "make_env.sh" in msg and "PYTHONPATH" in msg           # names the fix, and says PYTHONPATH is not it


def test_a_symlinked_location_is_judged_by_its_real_path(tmp_path):
    real = tmp_path / "elsewhere" / "agentic_os"
    real.mkdir(parents=True)
    (real / "__init__.py").write_text("")
    link = tmp_path / "repo-lookalike"
    link.symlink_to(tmp_path / "elsewhere")
    assert _env_guard.mismatch(link, str(real / "__init__.py")) is None          # the same directory reached through a link is the same place
    assert _env_guard.mismatch(ROOT, str(link / "agentic_os" / "__init__.py")) is not None


def test_the_ambient_resolution_is_what_a_subprocess_in_another_cwd_sees():
    found = _env_guard.ambient_location()
    assert found and Path(found).resolve().is_relative_to(ROOT.resolve())


# --- the whole thing, end to end: a real pytest session with a foreign agentic_os first on the child's path ---------------------------------------------
def _session(tmp_path: Path, env_extra: dict):
    probe = ROOT / "tests" / "test_required_test_dependencies.py"
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q", str(probe)], cwd=tmp_path,
                          capture_output=True, text=True, env={**os.environ, **env_extra})


def test_a_session_bound_to_another_worktree_fails_loudly_and_collects_nothing(tmp_path):
    foreign = tmp_path / "other-worktree"
    (foreign / "agentic_os").mkdir(parents=True)
    (foreign / "agentic_os" / "__init__.py").write_text("")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    r = _session(cwd, {"PYTHONPATH": str(foreign)})
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert str(foreign / "agentic_os" / "__init__.py") in out and str(ROOT) in out, out          # both paths
    assert "make_env.sh" in out, out                                                              # the fix
    assert "tests collected" not in out and "test_required_test_dependency_is_installed" not in out, out    # it never silently continued


def test_the_same_session_without_the_foreign_path_collects(tmp_path):
    r = _session(tmp_path, {})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "test_required_test_dependency_is_installed" in r.stdout


# --- the environment tool ---------------------------------------------------------------------------------------------------------------------------
def _fake_checkout(path: Path) -> Path:
    (path / "agentic_os").mkdir(parents=True)
    (path / "agentic_os" / "__init__.py").write_text("")
    (path / "pyproject.toml").write_text('[project]\nname = "agentic-os"\nversion = "0"\ndependencies = []\n')
    return path


def _bound_env(tmp_path: Path, name: str, worktree: Path) -> Path:
    """An environment directory whose venv (no pip: fast) imports agentic_os from ``worktree`` through a .pth file, like an editable install."""
    envdir = tmp_path / name
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(envdir / "venv")], check=True, capture_output=True)
    site = next((envdir / "venv" / "lib").glob("python*/site-packages"))
    (site / "agentic_os_editable.pth").write_text(str(worktree) + "\n")
    return envdir


def _make_env(*args: str, env=None):
    return subprocess.run(["bash", str(TESTENV / "make_env.sh"), *args], capture_output=True, text=True, env=env if env is not None else CLEAN)


def test_make_env_verification_passes_for_an_install_bound_to_the_worktree(tmp_path):
    wt = _fake_checkout(tmp_path / "wtA")
    envdir = _bound_env(tmp_path, "envA", wt)
    r = _make_env("--worktree", str(wt), "--env-dir", str(envdir), "--verify-only")
    assert r.returncode == 0, r.stderr
    assert str(wt) in r.stdout


def test_make_env_verification_catches_an_install_bound_elsewhere(tmp_path):
    a, b = _fake_checkout(tmp_path / "wtA"), _fake_checkout(tmp_path / "wtB")
    envdir = _bound_env(tmp_path, "envA", a)                      # the venv imports wtA ...
    r = _make_env("--worktree", str(b), "--env-dir", str(envdir), "--verify-only")        # ... but is used for wtB
    assert r.returncode == 1, r.stdout + r.stderr
    assert str(a / "agentic_os" / "__init__.py") in r.stderr and str(b) in r.stderr and "make_env.sh" in r.stderr
    assert "nested subprocess" in r.stderr                         # it also checked a subprocess started from another cwd


def test_make_env_refuses_pythonpath_and_an_env_dir_inside_the_worktree(tmp_path):
    wt = _fake_checkout(tmp_path / "wt")
    r = _make_env("--worktree", str(wt), "--env-dir", str(tmp_path / "e"), "--dry-run", env={**CLEAN, "PYTHONPATH": str(wt)})
    assert r.returncode == 1 and "PYTHONPATH" in r.stderr
    r = _make_env("--worktree", str(wt), "--env-dir", str(wt / "env"), "--dry-run")
    assert r.returncode == 1 and "inside the worktree" in r.stderr


def test_make_env_dry_run_creates_nothing_and_prints_the_pins():
    r = _make_env("--worktree", str(ROOT), "--env-dir", str(ROOT.parent / ("dry-run-" + ROOT.name)), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "duckdb==1.5.5" in r.stdout and "jsonschema" in r.stdout and "runtime-contracts" in r.stdout
    assert not (ROOT.parent / ("dry-run-" + ROOT.name) / "venv").exists()


def test_the_plan_pins_the_dev_extra_and_constrains_to_the_lockfile():
    spec = importlib.util.spec_from_file_location("envtool", TESTENV / "envtool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    plan = mod.plan(ROOT, ROOT / "uv.lock")
    assert "duckdb==1.5.5" in plan["index"] and any(r.startswith("jsonschema") for r in plan["index"]) and any(r.startswith("pytest") for r in plan["index"])
    assert "duckdb==1.5.5" in plan["constraints"]
    assert {g["name"] for g in plan["git"]} == {"runtime-contracts", "discovery-runtime", "knowledge-frontier"}


def test_run_tests_refuses_an_environment_made_for_another_worktree_or_another_history(tmp_path):
    spec = importlib.util.spec_from_file_location("envtool", TESTENV / "envtool.py")
    envtool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(envtool)
    wt = _fake_checkout(tmp_path / "wt")
    _git(wt, "init", "-q", "-b", "main")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "one")
    envdir = _bound_env(tmp_path, "env", wt)
    envtool.write_env(str(envdir / "venv" / "bin" / "python"), wt, envdir)
    rec = json.loads((envdir / "env.json").read_text())
    assert rec["worktree"] == str(wt.resolve()) and rec["git_head"] == _git(wt, "rev-parse", "HEAD") and rec["agentic_os_file"].startswith(str(wt.resolve()))
    assert envtool.check(envdir, wt) == []                                                # same worktree, same HEAD

    (wt / "later.txt").write_text("x")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "two")
    assert envtool.check(envdir, wt) == []                                                # a descendant of the recorded HEAD is still the same line of history

    other = _fake_checkout(tmp_path / "other")
    _git(other, "init", "-q", "-b", "main")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "elsewhere")
    problems = envtool.check(envdir, other)                                               # the other worktree must not use this environment
    assert any("is bound to the worktree" in p for p in problems), problems
    r = subprocess.run(["bash", str(TESTENV / "run_tests.sh"), "--env-dir", str(envdir), "--worktree", str(other), "--", "--version"], capture_output=True, text=True, env=CLEAN)
    assert r.returncode == 1 and "refusing to run" in r.stderr and str(wt) in r.stderr and str(other) in r.stderr, r.stderr

    _git(wt, "checkout", "-q", "--orphan", "unrelated")
    _git(wt, "commit", "-q", "-m", "unrelated history")
    problems = envtool.check(envdir, wt)
    assert any("not an ancestor" in p for p in problems), problems
