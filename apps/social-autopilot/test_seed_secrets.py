"""seed.py must not fall back to a known `changeme-*` credential: require env, or APP_DEMO_MODE (-> random)."""
from __future__ import annotations

import importlib

import pytest

seed = importlib.import_module("social-autopilot.seed")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("POSTIZ_API_KEY", "POSTIZ_USER_PASSWORD", "APP_DEMO_MODE"):
        monkeypatch.delenv(k, raising=False)
    seed._GENERATED.clear()


def test_no_changeme_literal_left_in_source():
    assert "changeme" not in open(seed.__file__).read()


@pytest.mark.parametrize("fn", ["ORG_API_KEY", "USER_PASSWORD"])
def test_requires_env_when_not_demo(fn):
    with pytest.raises(SystemExit):
        getattr(seed, fn)()


def test_env_value_is_used(monkeypatch):
    monkeypatch.setenv("POSTIZ_API_KEY", "k-from-env")
    monkeypatch.setenv("POSTIZ_USER_PASSWORD", "pw-from-env")
    assert seed.ORG_API_KEY() == "k-from-env" and seed.USER_PASSWORD() == "pw-from-env"


def test_demo_mode_generates_random_stable_per_run(monkeypatch):
    monkeypatch.setenv("APP_DEMO_MODE", "true")
    k, p = seed.ORG_API_KEY(), seed.USER_PASSWORD()
    assert k and p and k != p and "changeme" not in k + p
    assert seed.ORG_API_KEY() == k and seed.USER_PASSWORD() == p     # same value within one run
    seed._GENERATED.clear()
    assert seed.ORG_API_KEY() != k                                    # but never a fixed default
