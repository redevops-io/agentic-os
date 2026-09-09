"""The Projects API — the read/action surface the Projects UI renders. Exercised with the
sample projection provider (no Runtime wiring needed) via FastAPI's TestClient."""
from __future__ import annotations

from fastapi.testclient import TestClient

from agentic_os.projects_api import SampleProjectionProvider, create_app, sidekick_reply

client = TestClient(create_app(SampleProjectionProvider()))


def test_lists_projects():
    r = client.get("/api/projects")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "customer-ops"


def test_overview_composes_cross_runtime_projections():
    ov = client.get("/api/projects/customer-ops/overview").json()
    assert ov["project"]["name"] == "Customer Operations"
    assert {"attention", "missions", "workflows", "discovery", "apps"} <= set(ov)
    # every projection carries provenance for drill-down
    assert ov["missions"][0]["source_runtime"] == "mission"


def test_section_endpoints_return_expected_shapes():
    assert any(m["title"] == "Refund Sarah Chen" for m in client.get("/api/projects/p/missions").json())
    assert client.get("/api/projects/p/attention").json()[0]["kind"] == "approval"
    assert client.get("/api/projects/p/discovery").json()[0]["needsReview"] is True
    assert len(client.get("/api/projects/p/activity").json()) > 0
    wf = client.get("/api/projects/p/workflows").json()
    assert any(w["name"] == "Customer Refund Handling" for w in wf)


def test_apps_projection_has_the_provider_shape():
    apps = client.get("/api/projects/p/apps").json()
    providers = {a["provider"] for a in apps}
    assert "slack" in providers
    slack = next(a for a in apps if a["provider"] == "slack")
    assert {"display_name", "state", "health", "capabilities", "setup_url",
            "manual_steps", "credential_fields"} <= set(slack)


def test_sidekick_honours_the_context_contract():
    r = client.post("/api/sidekick", json={
        "ctx": {"projectId": "p", "section": "Workflows", "objectRef": "Customer Refund Handling"},
        "text": "make this require two approvers above $500",
    }).json()
    assert "Customer Refund Handling" in r["text"]
    assert r["actions"][0]["kind"] == "commit"


def test_sidekick_reply_is_deterministic_and_pure():
    # the reply helper is usable without HTTP (unit-level)
    r = sidekick_reply({"section": "Missions"}, "why does this need approval?")
    assert "tier-4" in r["text"].lower()
