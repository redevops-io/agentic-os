"""HTTP-level test of the content mission over the Projects API — the real UI contract end to end."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from starlette.testclient import TestClient  # noqa: E402

from agentic_os.content import (  # noqa: E402
    Channel, ContentBrief, ContentMissionRegistry, FakeGenerator, FakePublisher, PublishOutcome,
)
from agentic_os.content.app import create_content_app  # noqa: E402

_CH = (Channel.X, Channel.TIKTOK, Channel.LINKEDIN)


def test_projects_api_lists_details_and_approves_content_mission():
    reg = ContentMissionRegistry(owner="Alex", generator_factory=FakeGenerator,
                                 publisher_factory=lambda: FakePublisher(publishable=_CH))
    run = reg.open(ContentBrief(campaign_id="c1", subject="Metro Eng & Tech",
                                angle="an SMB that stopped missing local-gov contracts",
                                key_message="Right solicitations surfaced; one-tap approve.",
                                channels=_CH, cta="See how →"))
    client = TestClient(create_content_app(reg))

    missions = client.get("/api/projects/content-studio/missions").json()
    assert any(m["id"] == run.mission_id and m["state"] == "needs" for m in missions)

    detail = client.get(f"/api/projects/content-studio/missions/{run.mission_id}").json()
    assert any(a["source_id"] == "reel" and a.get("preview") for a in detail["context_used"])

    attention = client.get("/api/projects/content-studio/attention").json()
    assert attention and attention[0]["available_actions"] == ["Review", "Approve", "Reject"]

    # owner approves in the UI → publish gate resolves, content posts, attention clears
    resp = client.post(f"/api/projects/content-studio/missions/{run.mission_id}/approve",
                       json={"decision": "approve"}).json()
    assert resp["ok"] and all(d["status"] == PublishOutcome.PUBLISHED.value for d in resp["drafts"])
    assert client.get("/api/projects/content-studio/attention").json() == []


def test_reject_via_api_publishes_nothing():
    pub = FakePublisher(publishable=_CH)
    reg = ContentMissionRegistry(owner="Alex", generator_factory=FakeGenerator, publisher_factory=lambda: pub)
    run = reg.open(ContentBrief(campaign_id="c2", subject="Metro Eng & Tech", angle="a",
                                key_message="k", channels=_CH, cta="x"))
    client = TestClient(create_content_app(reg))
    resp = client.post(f"/api/projects/content-studio/missions/{run.mission_id}/approve",
                       json={"decision": "reject"}).json()
    assert resp["ok"] and resp["decision"] == "reject"
    assert pub.sent == []
