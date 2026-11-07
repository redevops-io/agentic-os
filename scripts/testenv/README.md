# Test environment bound to one worktree

Several tests start a subprocess (`[sys.executable, "-m", "pytest", ...]` or `-c`, often from a temporary directory). Such a child imports `agentic_os`
from whatever the interpreter's environment has installed. If that is an install from ANOTHER worktree, the child runs the other worktree's code while
the in-process tests (where pytest has put this repository on `sys.path`) run this one. The failures look unrelated to the change under test.

`make_env.sh` creates an environment whose only `agentic_os` is an editable install of one given worktree and verifies it; `run_tests.sh` runs pytest in it
under `env -i` and refuses an environment made for a different worktree. `tests/conftest.py` re-checks the binding at session start (below).

## Create

```
env -i PATH=/opt/homebrew/bin:/usr/bin:/bin HOME=<throwaway> LANG=C.UTF-8 \
  scripts/testenv/make_env.sh --worktree <worktree> --env-dir <directory outside the repository> --side-deps <dir>
```

- `--env-dir` must be outside the worktree. It receives `venv/`, `env.json`, `home/` (throwaway HOME) and `tmp/` (throwaway TMPDIR).
- Requirements are the project's base dependencies and its `dev` extra (`pyproject.toml`: `pytest`, `jsonschema`, `duckdb==1.5.5`, ...), installed from the
  package index (or the pip cache) with every version constrained to `uv.lock`. The worktree is then installed with `pip install -e --no-deps`
  (the editable build also places the pinned Projects UI bundle, from the artifact in the repository, in the git-ignored `agentic_os/projects_ui/`).
- The three dependencies that `pyproject.toml` pins by git tag (`runtime-contracts`, `discovery-runtime`, `knowledge-frontier`) are not fetched from the
  network. `--side-deps` names a directory laid out like `site-packages` (`<name>-<version>.dist-info` next to the package); the versions must equal the pins.
- `--python` selects the interpreter (default: the first of `python3.12`, `3.13`, `3.14`, `3.11`, `python3` that is at least 3.11). `--force` recreates an
  existing venv. `--dry-run` prints the plan and creates nothing. `--verify-only` only checks that an existing `<env-dir>/venv` is bound to the worktree.
- `PYTHONPATH` set in the calling environment is refused.

### What is verified

With the venv's interpreter, from a neutral working directory (not the worktree): `agentic_os` resolves to a file inside the worktree; a subprocess that
this process starts in yet another directory resolves it there too; the installed distribution's `direct_url.json` points at the worktree; `PYTHONPATH` is
empty. Any failure exits non-zero and names the found path, the worktree, and the fix.

`env.json` records the worktree path, git branch and HEAD, Python version and executable, the resolved `agentic_os` path (in-process and nested), versions of the
main dependencies, and every installed distribution.

## Run

```
scripts/testenv/run_tests.sh --env-dir <env-dir> [--worktree <path>] [--tmpdir <dir>] [--env NAME=VALUE]... [--] [pytest args]    # default args: -q tests
```

The child process gets only `PATH` (venv `bin` first), `HOME=<env-dir>/home`, `TMPDIR=<env-dir>/tmp` (or `--tmpdir`), `LANG=C.UTF-8`,
`PYTHONDONTWRITEBYTECODE=1` and any `--env` pairs (`PYTHONPATH`, `PYTHONHOME` and `VIRTUAL_ENV` are refused). It refuses to run when `env.json` names another
worktree, when the recorded HEAD is not an ancestor of (or equal to) the worktree's HEAD, or when the binding no longer holds.
The suite expects the Projects UI bundle in `agentic_os/projects_ui/` (the editable install places it).

## The session guard

`tests/conftest.py` calls `tests/_env_guard.py` in `pytest_configure`: it resolves `agentic_os` the way a test subprocess would (a fresh interpreter in an empty
directory, this process's environment) and stops the session with exit status 4, naming both paths and the fix, when the result is not inside this repository
or `agentic_os` was already imported from elsewhere. There is no override. It is tested in `tests/test_env_guard.py`, including a real pytest session with a
foreign `agentic_os` first on the child's `PYTHONPATH`, and the shell-level verification of an environment bound to another worktree.
