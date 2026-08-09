"""The version a consumer reads must be the version that was released.

Three releases across this runtime family shipped with `__version__` disagreeing
with the packaging metadata — `runtime-contracts` v0.2.0, and `agentic-os`
v0.2.0 and v0.2.1. Every one was invisible from inside the repository that
released it, because nothing here reads its own version or builds its own
package, and obvious in the first command of the consumer that installed it.

A version string that lies is worse than no version string, because a version
string is checked. This makes the check local, where the fix is.
"""
from __future__ import annotations

import re
from pathlib import Path

import agentic_os

ROOT = Path(__file__).resolve().parent.parent


def declared() -> str:
    return re.search(r'^version = "([^"]+)"',
                     (ROOT / "pyproject.toml").read_text(), re.M).group(1)


def test_the_module_version_matches_the_package_version():
    assert agentic_os.__version__ == declared()


def test_direct_references_are_permitted_by_the_build_backend():
    """v0.2.0 built here and installed nowhere. `runtime-contracts` is pinned by
    git tag, which hatchling calls a direct reference and refuses unless this is
    set — and the tests never noticed, because they run from the source tree."""
    text = (ROOT / "pyproject.toml").read_text()
    if "git+" in text:
        assert re.search(r"^allow-direct-references\s*=\s*true", text, re.M), (
            "a git-pinned dependency without "
            "`[tool.hatch.metadata] allow-direct-references = true` makes every "
            "tag unusable by any consumer")
