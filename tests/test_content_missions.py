"""Content distribution as a governed Mission — generate → preview → owner approval gate → publish.

Offline throughout (Fake generator/publisher): the mission generates a concept + reel preview + per-channel
drafts, parks at the single human-approval gate, and only publishes after approval. A channel with no
publisher degrades to a manual handoff, never a silent drop.
"""
from __future__ import annotations

from agentic_os.mission.types import MissionState
from agentic_os.content import (
    Channel, ContentBrief, ContentMissionRun, FakeGenerator, FakePublisher, MultiPublisher, PublishOutcome,
)

_CHANNELS = (Channel.X, Channel.TIKTOK, Channel.LINKEDIN)


def _brief() -> ContentBrief:
    return ContentBrief(
        campaign_id="metro-eng-001", subject="Metro Eng & Tech",
        angle="an SMB that stopped missing local-government contracts",
        key_message="Metro Eng now gets the right Miami-Dade solicitations surfaced and one-tap approves a bid.",
        channels=_CHANNELS, cta="See how →",
        evidence_ids=("mission:revenue:E26SP01", "obs:src-miami-dade-informs:detail:0d4"))


def test_mission_parks_at_publish_gate_with_previews():
    run = ContentMissionRun(_brief(), owner="Alex",
                            generator=FakeGenerator(), publisher=FakePublisher(publishable=_CHANNELS))
    assert run.state is MissionState.WAITING_HUMAN               # parked at the single publish gate
    pv = run.preview()
    assert pv["awaiting_approval"] and pv["concept"]["hook"]
    assert pv["reel_preview_url"].endswith(".mp4")              # a rendered video preview to review
    drafted = {d["channel"] for d in pv["drafts"]}
    assert "x" in drafted and "linkedin" in drafted            # per-channel copy generated pre-gate
    # nothing has been published yet — the gate is holding
    assert all(d["status"] == "draft" for d in pv["drafts"])


def test_owner_approval_publishes_all_channels():
    pub = FakePublisher(publishable=_CHANNELS)
    run = ContentMissionRun(_brief(), owner="Alex", generator=FakeGenerator(), publisher=pub)
    result = run.approve()
    assert run.state is MissionState.SUCCEEDED
    assert {d.channel for d in result.drafts} == set(_CHANNELS)
    assert all(d.status == PublishOutcome.PUBLISHED.value and d.post_url for d in result.drafts)
    assert len(pub.sent) == 3                                    # each channel actually posted
    assert result.timeline                                       # replayable


def test_reject_does_not_publish():
    pub = FakePublisher(publishable=_CHANNELS)
    run = ContentMissionRun(_brief(), owner="Alex", generator=FakeGenerator(), publisher=pub)
    run.reject()
    assert run.state is not MissionState.SUCCEEDED
    assert pub.sent == []                                        # nothing posted on reject


def test_channel_without_publisher_is_manual_handoff_not_dropped():
    # only TikTok is publishable; X + LinkedIn have no publisher → manual handoff with the approved copy
    pub = MultiPublisher([FakePublisher(publishable=(Channel.TIKTOK,))])
    run = ContentMissionRun(_brief(), owner="Alex", generator=FakeGenerator(), publisher=pub)
    result = run.approve()
    by_ch = {d.channel: d for d in result.drafts}
    assert by_ch[Channel.TIKTOK].status == PublishOutcome.PUBLISHED.value
    assert by_ch[Channel.X].status == PublishOutcome.MANUAL_HANDOFF.value
    assert by_ch[Channel.LINKEDIN].status == PublishOutcome.MANUAL_HANDOFF.value
    assert by_ch[Channel.X].text                                 # the approved copy is still handed off


def test_template_registered():
    from agentic_os.mission import templates
    intent = templates.get("content_distribution", "m-1")
    assert intent is not None
    outcomes = {s.outcome for s in intent.steps}
    assert {"concept_generated", "reel_rendered", "content_published"} <= outcomes
