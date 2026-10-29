"""The root ``Dockerfile`` copies every file its ``pip install .`` needs, and the build context does not exclude any of them.

``pip install .`` runs the wheel build, which needs what ``pyproject.toml`` names: the readme, the package directory and the custom build hook
(``hatch_build.py``). The hook reads ``scripts/`` and ``packaging/`` unless ``AGENTIC_OS_SKIP_UI_FETCH=1`` is set. A Dockerfile that misses one of these
stops at build time ("Build script does not exist: hatch_build.py"), which no other test notices because nothing builds the image in CI.
This is a static check (no Docker needed): the COPY sources exist, ``.dockerignore`` does not drop them, and the build-time needs are covered.
"""
from __future__ import annotations

import fnmatch
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def _logical_lines(text: str):
    cur = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            cur += line[:-1] + " "
            continue
        yield cur + line
        cur = ""


def _copy_sources() -> list[str]:
    out = []
    for line in _logical_lines(DOCKERFILE):
        m = re.match(r"COPY\s+(?:--\S+\s+)*(.+)", line, re.I)
        if m:
            *srcs, _dest = m.group(1).split()
            out += [s.rstrip("/") for s in srcs]
    return out


def _covered(path: str, sources: list[str]) -> bool:
    """True if ``path`` (relative to the repo root) is copied: named directly or inside a copied directory."""
    p = Path(path)
    return any(p == Path(s) or Path(s) in p.parents or s == "." for s in sources)


def _ignored(path: str) -> bool:
    patterns = [l.strip() for l in (ROOT / ".dockerignore").read_text().splitlines() if l.strip() and not l.startswith("#")]
    parts = Path(path).parts
    for pat in patterns:
        pat = pat.rstrip("/")
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            if fnmatch.fnmatch(sub, pat) or fnmatch.fnmatch(sub, pat.removeprefix("**/")) or fnmatch.fnmatch(parts[i - 1], pat.removeprefix("**/")):
                return True
    return False


def _build_needs() -> list[str]:
    needs = [PYPROJECT["project"]["readme"], "pyproject.toml"]
    needs += PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    hook = PYPROJECT["tool"]["hatch"]["build"].get("hooks", {}).get("custom")
    if hook is not None:
        needs.append(hook.get("path", "hatch_build.py"))
    return needs


def test_the_copy_sources_exist():
    for s in _copy_sources():
        assert (ROOT / s).exists(), f"Dockerfile COPYs {s}, which is not in the repository"


def test_every_file_the_wheel_build_needs_is_copied():
    sources = _copy_sources()
    missing = [n for n in _build_needs() if not _covered(n, sources)]
    assert not missing, f"the Dockerfile's `pip install .` needs {missing}, which are not COPY'd"


def test_the_build_context_does_not_exclude_what_is_copied():
    dropped = [s for s in _copy_sources() + _build_needs() if _ignored(s)]
    assert not dropped, f".dockerignore excludes files the Dockerfile needs: {dropped}"


def test_the_hook_inputs_are_copied_or_the_ui_fetch_is_skipped():
    """hatch_build.py reads scripts/fetch_projects_ui.py and packaging/projects_ui.lock.json (and downloads a release) unless it is told to skip."""
    hook = (ROOT / "hatch_build.py").read_text()
    reads = [p for p in ("scripts", "packaging") if f'"{p}"' in hook]
    assert reads, "the hook no longer reads scripts/ or packaging/: update this test"
    install_lines = [l for l in _logical_lines(DOCKERFILE) if "pip install" in l]
    assert install_lines, "the Dockerfile no longer runs pip install"
    skips = all("AGENTIC_OS_SKIP_UI_FETCH=1" in l for l in install_lines) or re.search(r"^ENV\s+AGENTIC_OS_SKIP_UI_FETCH=1", DOCKERFILE, re.M)
    sources = _copy_sources()
    assert skips or all(_covered(p, sources) for p in reads), f"the build hook reads {reads}: COPY them or set AGENTIC_OS_SKIP_UI_FETCH=1 for the install"


def test_the_container_starts_the_control_plane():
    cmd = [l for l in _logical_lines(DOCKERFILE) if l.startswith("CMD")]
    assert cmd and "agentic_os.control_plane:app" in cmd[-1] and "uvicorn" in cmd[-1], cmd
