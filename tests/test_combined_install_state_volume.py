"""The combined install mounts a named volume into the Projects container, which runs as an unprivileged user. A named
volume takes its ownership from the image's directory, so the image has to create each such directory for that user, or
the service cannot open its stores and never becomes healthy."""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("compose", ["deploy/combined/compose.yml", "deploy/combined/compose.bundle.yml"])
def test_every_named_volume_of_projects_lands_on_a_directory_the_images_user_owns(compose):
    spec = yaml.safe_load((ROOT / compose).read_text())
    named = set(spec.get("volumes") or {})
    targets = [str(m).split(":")[1] for m in spec["services"]["projects"].get("volumes", []) if str(m).split(":")[0] in named]
    assert targets, "the projects service keeps its state on a named volume"
    dockerfile = (ROOT / "Dockerfile.projects").read_text()
    user = re.findall(r"^USER (\d+):(\d+)$", dockerfile, re.M)
    assert user and user[-1] != ("0", "0"), "the image runs unprivileged"
    uid, gid = user[-1]
    before_user = dockerfile.split(f"\nUSER {uid}:{gid}")[0].replace("\\\n", " ")
    for target in targets:
        assert re.search(rf"mkdir [^&\n]*(?<!\S){re.escape(target)}(?!\S)", before_user), f"the image does not create {target}"
        assert re.search(rf"chown {uid}:{gid} [^&\n]*(?<!\S){re.escape(target)}(?!\S)", before_user), f"{target} is not owned by the image's user"
