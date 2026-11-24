"""The bundle that is PUBLISHED carries the product versions the compose file and the example environment name.

Bundle 0.1.6 was published with Edge Sentinel 0.6.3 while the compose default said 0.6.5: the publish workflow sets the image tags in its
own environment, and `docker compose publish --resolve-image-digests` pins whatever those resolve to. The finding loop then failed on the
published bundle at its last step (0.6.3 has no resolution endpoint). These tests keep the three places together.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = yaml.safe_load((ROOT / ".github/workflows/oci-redevops-ts.yml").read_text())
COMPOSE = (ROOT / "deploy/combined/compose.bundle.yml").read_text()
EXAMPLE = (ROOT / "deploy/combined/.env.bundle.example").read_text()


def _workflow_env() -> dict:
    env = {}
    for job in WF["jobs"].values():
        env.update(job.get("env") or {})
    return env


def _default(var: str) -> str:
    m = re.search(r"\$\{" + var + r":-([^}]+)\}", COMPOSE)
    assert m, f"compose.bundle.yml has no default for {var}"
    return m.group(1)


def _example(var: str) -> str:
    m = re.search(rf"^{var}=(.+)$", EXAMPLE, re.M)
    assert m, f".env.bundle.example does not set {var}"
    return m.group(1).strip()


def test_the_publish_workflow_pins_the_versions_the_compose_file_defaults_to():
    env = _workflow_env()
    for var in ("EDGE_SENTINEL_IMAGE_TAG", "QA_IMAGE_TAG"):
        assert str(env.get(var)) == _default(var) == _example(var), (var, env.get(var), _default(var), _example(var))


def test_the_bootstrap_default_is_the_bundle_the_example_names():
    boot = (ROOT / "deploy/combined/bundle-bootstrap.sh").read_text()
    tag = _example("BUNDLE_TAG")
    assert f'redevops-ts:{tag}"' in boot and _example("PROJECTS_IMAGE_TAG") == f"bundle-{tag}"
