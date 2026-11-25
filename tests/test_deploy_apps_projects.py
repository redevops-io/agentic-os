"""The installer stack (deploy/apps) ships Projects with the dashboards: a signed image pinned by digest, behind the operator
sign-in, in the production profile (no sample data), on its own port (the bundled UI is built for the site root)."""
from pathlib import Path

import yaml

APPS = Path(__file__).resolve().parents[1] / "deploy" / "apps"


def _svc():
    return yaml.safe_load((APPS / "compose.yml").read_text())["services"]["projects"]


def test_projects_is_pinned_by_digest_and_gated():
    s = _svc()
    name, _, digest = s["image"].partition("@sha256:")
    assert name == "ghcr.io/redevops-io/redevops-projects" and len(digest) == 64
    assert s["ports"] == ["${PROJECTS_HTTP_PORT:-8090}:8787"]
    env = dict(e.split("=", 1) for e in s["environment"])
    assert env["PROJECTS_OPERATOR_GATE"] == "1"
    assert env["OPERATOR_BASIC_AUTH"].startswith("${OUTREACH_BASIC_AUTH:?")          # compose refuses to start without the credential
    assert env["AGENTIC_OS_PROFILE"] == "production"
    assert "PROJECTS_BASE_PATH" not in env and "PROJECTS_UI_DIR" not in env      # the bundled UI is built for "/": a base path breaks its assets
    assert not [k for k, v in env.items() if k.endswith(("_KEY", "_PASSWORD", "_SECRET")) and not v.startswith("${")]   # no literal secret


def test_the_dashboards_file_is_valid_and_mounted():
    from agentic_os.dashboards.config import parse_config
    cfg = parse_config(yaml.safe_load((APPS / "dashboards.yaml").read_text()))
    assert "main" in cfg.projects
    mounts = _svc()["volumes"]
    assert "./dashboards.yaml:/config/dashboards.yaml:ro" in mounts
