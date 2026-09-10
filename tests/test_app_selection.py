"""Enabled-app selection — a deployment offers only a subset of apps via $PROJECTS_APPS.

The filter is evaluated per request (it reads the env at projection time), so one TestClient
serves every case; only the environment changes between them. Unset = all apps, so the default
behaviour is unchanged.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentic_os.projects_api import (
    SampleProjectionProvider,
    create_app,
    enabled_apps,
    enabled_selection,
)

client = TestClient(create_app(SampleProjectionProvider()))


# ── the pure helper ──────────────────────────────────────────────────────────────
def _apps():
    return [{"provider": "google"}, {"provider": "slack"}, {"provider": "stripe"}]


def test_enabled_apps_no_selection_returns_all():
    apps = _apps()
    assert enabled_apps(apps, None) == apps
    assert enabled_apps(apps, set()) == apps


def test_enabled_apps_filters_to_selection():
    got = enabled_apps(_apps(), {"google", "slack"})
    assert [a["provider"] for a in got] == ["google", "slack"]


def test_enabled_apps_ignores_unknown_ids():
    got = enabled_apps(_apps(), {"slack", "does-not-exist"})
    assert [a["provider"] for a in got] == ["slack"]


def test_enabled_apps_all_unknown_yields_empty():
    assert enabled_apps(_apps(), {"nope", "nada"}) == []


# ── env parsing ────────────────────────────────────────────────────────────────
def test_enabled_selection_unset_is_none(monkeypatch):
    monkeypatch.delenv("PROJECTS_APPS", raising=False)
    assert enabled_selection() is None


def test_enabled_selection_empty_is_none(monkeypatch):
    monkeypatch.setenv("PROJECTS_APPS", "  ,  , ")
    assert enabled_selection() is None


def test_enabled_selection_trims_and_splits(monkeypatch):
    monkeypatch.setenv("PROJECTS_APPS", " google , slack ,stripe")
    assert enabled_selection() == {"google", "slack", "stripe"}


# ── end to end through the API (overview + /apps) ────────────────────────────────
def test_unset_projects_all_apps(monkeypatch):
    monkeypatch.delenv("PROJECTS_APPS", raising=False)
    providers = {a["provider"] for a in client.get("/api/projects/p/apps").json()}
    # unset = no filter → the full app surface is offered
    assert {"slack", "stripe", "hubspot", "gmail"} <= providers
    # the overview's embedded apps are likewise unfiltered. NOTE they are a *different* projection:
    # /apps prefers the live connector setup-guides when the [connectors] plugin is installed, while
    # the overview uses the sample surface — so the overview apps are a subset of /apps, not equal.
    ov = {a["provider"] for a in client.get("/api/projects/customer-ops/overview").json()["apps"]}
    assert {"slack", "stripe", "hubspot"} <= ov <= providers


def test_subset_projects_only_enabled_apps(monkeypatch):
    monkeypatch.setenv("PROJECTS_APPS", "slack,stripe")
    apps = client.get("/api/projects/p/apps").json()
    assert {a["provider"] for a in apps} == {"slack", "stripe"}
    # the overview's embedded apps are filtered the same way
    ov = client.get("/api/projects/customer-ops/overview").json()
    assert {a["provider"] for a in ov["apps"]} == {"slack", "stripe"}


def test_unknown_ids_ignored_gracefully(monkeypatch):
    monkeypatch.setenv("PROJECTS_APPS", "slack,ghost-app")
    apps = client.get("/api/projects/p/apps").json()
    assert {a["provider"] for a in apps} == {"slack"}


def test_single_app_selection(monkeypatch):
    monkeypatch.setenv("PROJECTS_APPS", "stripe")
    apps = client.get("/api/projects/p/apps").json()
    assert [a["provider"] for a in apps] == ["stripe"]
