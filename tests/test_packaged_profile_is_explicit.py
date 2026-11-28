"""A packaged deployment never runs with an assumed profile.

Unset `AGENTIC_OS_PROFILE` used to mean `development` (authority not enforced) everywhere, with a log line. In a shipped image that made a
forgotten setting equal to "enforcement off". Shipped images now carry `AGENTIC_OS_PACKAGED=1`, and there an unset profile is a startup error.
"""
import pathlib
import re

import pytest

from agentic_os.mission import profile as P

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHIPPED_IMAGES = ("Dockerfile", "Dockerfile.projects", "deploy/sidekick-devops/Dockerfile")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (P.ENV_PROFILE, P.ENV_PACKAGED, "AGENTIC_OS_REQUIRE_AUTHORITY"):
        monkeypatch.delenv(k, raising=False)


def test_a_packaged_deployment_without_a_profile_does_not_start(monkeypatch):
    monkeypatch.setenv(P.ENV_PACKAGED, "1")
    with pytest.raises(P.ProfileError, match="packaged deployment"):
        P.profile()
    with pytest.raises(P.ProfileError):
        P.assert_authority(None, where="MissionRuntime")          # what a service calls at start


@pytest.mark.parametrize("choice", ["production", "development"])
def test_a_packaged_deployment_runs_the_profile_it_was_given(monkeypatch, choice):
    monkeypatch.setenv(P.ENV_PACKAGED, "1")
    monkeypatch.setenv(P.ENV_PROFILE, choice)
    assert P.profile() == choice


def test_packaged_production_still_needs_a_durable_guard(monkeypatch):
    monkeypatch.setenv(P.ENV_PACKAGED, "1")
    monkeypatch.setenv(P.ENV_PROFILE, "production")
    with pytest.raises(P.ProfileError, match="durable"):
        P.assert_authority(None, where="MissionRuntime")


def test_the_old_alias_counts_as_an_explicit_production_choice(monkeypatch):
    monkeypatch.setenv(P.ENV_PACKAGED, "1")
    monkeypatch.setenv("AGENTIC_OS_REQUIRE_AUTHORITY", "1")
    assert P.profile() == "production"


def test_a_source_checkout_still_defaults_to_development(monkeypatch):
    assert P.profile() == "development" and not P.packaged()


def test_every_shipped_image_declares_itself_packaged_and_sets_no_profile_of_its_own():
    for f in SHIPPED_IMAGES:
        text = (ROOT / f).read_text()
        assert re.search(r"^ENV .*AGENTIC_OS_PACKAGED=1", text, re.M), f"{f} must set AGENTIC_OS_PACKAGED=1"
        assert not re.search(r"^ENV .*AGENTIC_OS_PROFILE=", text, re.M), f"{f} must not choose a profile for the deployer"


def test_the_combined_install_selects_the_enforced_profile_with_durable_stores():
    for f in ("deploy/combined/compose.yml", "deploy/combined/compose.bundle.yml"):
        text = (ROOT / f).read_text()
        assert "${AGENTIC_OS_PROFILE:-production}" in text and "/state/authority.db" in text and "/state/replay.db" in text, f
        assert "projects-state:/state" in text, f"{f}: the stores must live on a volume, not in the container layer"


def test_the_sidekick_compose_requires_the_profile_instead_of_defaulting_it():
    text = (ROOT / "deploy/sidekick-devops/docker-compose.yml").read_text()
    assert "${AGENTIC_OS_PROFILE:?" in text
    assert re.search(r"^AGENTIC_OS_PROFILE=(development|production)$", (ROOT / "deploy/sidekick-devops/.env.example").read_text(), re.M)
