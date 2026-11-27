"""An app image installs its requirements with git already present when one of them resolves a dependency from a git
repository (context-runtime does). No workflow builds these images, so this is what notices."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

APPS = Path(__file__).resolve().parents[1] / "apps"
NEEDS_GIT = re.compile(r"^(context-runtime\b|.*git\+)", re.M)
IMAGES = sorted(d for d in APPS.iterdir() if (d / "Dockerfile").is_file() and (d / "requirements.txt").is_file()
                and NEEDS_GIT.search((d / "requirements.txt").read_text()))


def test_there_are_images_to_check():
    assert len(IMAGES) >= 10, [d.name for d in IMAGES]


@pytest.mark.parametrize("app", IMAGES, ids=lambda d: d.name)
def test_git_is_installed_no_later_than_the_requirements(app):
    steps = re.sub(r"\\\n", " ", (app / "Dockerfile").read_text()).splitlines()          # one RUN per line
    runs = [s for s in steps if s.startswith("RUN ")]
    requirements = next((i for i, s in enumerate(runs) if re.search(r"pip install [^&]*-r requirements\.txt", s)), None)
    if requirements is None:
        pytest.skip("this image does not install requirements.txt")
    git = next((i for i, s in enumerate(runs) if re.search(r"apt-get install [^&]*\bgit\b", s)), None)
    assert git is not None and git <= requirements, f"{app.name}: git comes after the requirements that need it"
    if git == requirements:
        assert runs[git].index(" git") < runs[git].index("-r requirements.txt")
