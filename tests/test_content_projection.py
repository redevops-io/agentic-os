"""Content missions surfaced in the Projects UI contracts — render + approve.

The Projects UI reads a ProjectionProvider (missions / mission_detail / attention). These assert a live
content mission projects into exactly those shapes — concept + reel preview + per-channel drafts as
`context_used` artifacts (with `preview` URLs), and the pending publish gate as an attention item with
Review/Approve/Reject — and that approving through the registry resolves the gate and clears attention.
"""
from __future__ import annotations

from agentic_os.content import (
    Channel, ContentBrief, ContentMissionRegistry, ContentProjectionProvider, FakeGenerator,
    FakePublisher,
)

_CHANNELS = (Channel.X, Channel.TIKTOK, Channel.LINKEDIN)


def _registry() -> ContentMissionRegistry:
    return ContentMissionRegistry(
        owner="Alex", generator_factory=FakeGenerator,
        publisher_factory=lambda: FakePublisher(publishable=_CHANNELS))


def _brief() -> ContentBrief:
    return ContentBrief(campaign_id="metro-eng-001", subject="Metro Eng & Tech",
                        angle="an SMB that stopped missing local-gov contracts",
                        key_message="Right Miami-Dade solicitations surfaced; one-tap approve a bid.",
                        channels=_CHANNELS, cta="See how →")


def test_mission_projects_into_ui_detail_shape_with_previews():
    reg = _registry()
    run = reg.open(_brief())
    prov = ContentProjectionProvider(reg)

    listed = prov.missions("content-studio")
    assert listed and listed[0]["id"] == run.mission_id and listed[0]["state"] == "needs"
    assert listed[0]["source_runtime"] == "mission"                 # provenance shape matches projects_api

    detail = prov.mission_detail("content-studio", run.mission_id)
    assert detail["summary"]["id"] == run.mission_id
    # steps carry the publish gate as 'waiting'
    publish = next(s for s in detail["steps"] if s["capability"] == "content.publish")
    assert publish["status"] == "waiting" and publish["tier"] == 4
    # context_used artifacts include the reel preview URL and per-channel drafts
    kinds = {a["source_id"] for a in detail["context_used"]}
    assert "concept" in kinds and "reel" in kinds
    reel = next(a for a in detail["context_used"] if a["source_id"] == "reel")
    assert reel["preview"].endswith(".mp4")


def test_pending_gate_shows_in_attention_then_clears_on_approve():
    reg = _registry()
    run = reg.open(_brief())
    prov = ContentProjectionProvider(reg)

    att = prov.attention("content-studio")
    assert len(att) == 1
    assert att[0]["kind"] == "approval" and att[0]["available_actions"] == ["Review", "Approve", "Reject"]
    assert att[0]["source_runtime"] == "governance"

    # approve through the registry → gate resolves, mission completes, attention clears
    reg.approve(run.mission_id)
    assert prov.attention("content-studio") == []
    assert prov.missions("content-studio")[0]["state"] == "completed"
    detail = prov.mission_detail("content-studio", run.mission_id)
    assert next(s for s in detail["steps"] if s["capability"] == "content.publish")["status"] == "done"


def test_projection_satisfies_the_provider_surface():
    # the methods the Projects UI calls all return without error on an empty project
    prov = ContentProjectionProvider(_registry())
    assert isinstance(prov.projects(), list)                         # projects() takes no project id
    for m in ("workflows", "discovery", "apps", "sources", "activity"):
        assert isinstance(getattr(prov, m)("content-studio"), list)
    assert isinstance(prov.overview("content-studio"), dict)
    assert isinstance(prov.priorities("content-studio"), dict)
