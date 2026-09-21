"""Phase 9 — Projects API endpoints for the gateway cards + Sidekick intent resolution."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from agentic_os.projects_api import create_app, sidekick_reply  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(create_app())


# ── projection endpoints ─────────────────────────────────────────────────────────────
def test_social_intelligence_endpoint_returns_projection(client):
    r = client.get("/api/projects/redevops-demo/social-intelligence")
    assert r.status_code == 200
    body = r.json()
    assert body["mission"] == "Social Intelligence"
    statuses = {s["source"]: s["status"] for s in body["sources"]}
    assert statuses["Reddit"].startswith("AVAILABLE")
    assert statuses["Meta/Muse"].startswith("UNAVAILABLE")
    # UNKNOWN survives to the wire
    assert body["opportunities"]["unknown_commercial_intent"] >= 1
    assert body["data_source"].startswith("fixture")


def test_deployment_inspection_endpoint_degrades_gracefully_when_soc_unreachable(client, monkeypatch):
    monkeypatch.setenv("SENTINEL_URL", "http://127.0.0.1:9")   # nothing listening → all reads fail
    r = client.get("/api/projects/redevops-demo/deployment-inspection")
    assert r.status_code == 200
    body = r.json()
    assert body["mission"] == "Deployment Inspection"
    assert body["connected"] is False and body["finding_count"] == 0
    assert body["governed_action"] is None                     # no finding → nothing to govern
    assert body["boundary"]["remediation"].startswith("simulated")


# ── Sidekick intent resolution (resolve + hand off, no workflow inside Sidekick) ─────
def test_sidekick_resolves_inspect_deployments_to_navigate_action():
    reply = sidekick_reply({"project": "redevops-demo"}, "Inspect current ReDevOps demo deployments")
    assert reply["topic"] == "Deployment Inspection"
    acts = reply["actions"]
    assert acts and acts[0]["kind"] == "navigate" and acts[0]["ref"] == "inspection"


def test_sidekick_resolves_social_intel_query():
    reply = sidekick_reply({}, "find recent discussions where founders are struggling with RAG reliability")
    assert reply["topic"] == "Social Intelligence"
    assert reply["actions"][0]["kind"] == "navigate" and reply["actions"][0]["ref"] == "social"


def test_sidekick_explanatory_phrasing_does_not_trigger_the_mission():
    # "what is deployment inspection" is a question, not a goal → must NOT hand off a navigate action
    reply = sidekick_reply({}, "what is deployment inspection")
    refs = [a.get("ref") for a in reply.get("actions", [])]
    assert "inspection" not in refs
