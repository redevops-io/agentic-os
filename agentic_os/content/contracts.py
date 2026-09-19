"""Content Missions — the contracts for content distribution as a governed mission.

A `ContentBrief` normalizes "make + distribute content for this angle" the way a `RevenueOpportunity`
normalizes a revenue signal. The mission generates a concept + per-channel drafts + media, parks at a
single human-approval gate, and publishes only what the owner approves. Nothing here reaches a live
social account without that approval — the publish step is the one side-effecting, gated node.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Channel(str, Enum):
    X = "x"                       # X/Twitter — text post or thread
    LINKEDIN = "linkedin"         # LinkedIn — text post
    TIKTOK = "tiktok"             # short-form vertical video
    INSTAGRAM = "instagram"       # short-form vertical video (Reels)
    YOUTUBE_SHORTS = "youtube_shorts"  # short-form vertical video

    @property
    def is_video(self) -> bool:
        return self in (Channel.TIKTOK, Channel.INSTAGRAM, Channel.YOUTUBE_SHORTS)


class PublishOutcome(str, Enum):
    PUBLISHED = "published"        # posted live to the channel
    MANUAL_HANDOFF = "manual_handoff"  # no publisher for this channel → approved copy handed to the owner
    FAILED = "failed"


@dataclass
class ContentConcept:
    """The generated creative concept the per-channel drafts derive from."""
    hook: str = ""
    narrative: str = ""
    key_points: tuple[str, ...] = ()


@dataclass
class ChannelDraft:
    """One channel's draft — text and/or a media preview URL — plus its publish result."""
    channel: Channel
    text: str = ""
    media_url: str = ""            # preview/render URL (reel or image) for owner review + publishing
    status: str = "draft"         # draft | approved | published | manual_handoff | failed
    post_id: str = ""
    post_url: str = ""
    error: str = ""


@dataclass
class ContentBrief:
    """The one shape a content campaign normalizes into before entering the mission machinery."""
    campaign_id: str
    subject: str                  # e.g. "Metro Eng & Tech"
    angle: str                    # the marketing angle/story
    key_message: str
    channels: tuple[Channel, ...]
    cta: str = ""
    owner: str = ""
    # lineage: references to the source story this content is built from (e.g. a revenue mission),
    # so the campaign stays evidence-native — the claims trace back to a real outcome, not invented.
    evidence_ids: tuple[str, ...] = ()


@dataclass
class ContentCampaign:
    """Mutable working state the fleet handlers coordinate through (mirrors how the revenue fleet closes
    over its opportunity). The mission world-state still records each step for replay/preview."""
    brief: ContentBrief
    concept: ContentConcept = field(default_factory=ContentConcept)
    reel_url: str = ""            # in-store URL (not externally viewable)
    reel_preview_url: str = ""    # presigned, externally viewable/downloadable render output
    reel_task_id: str = ""
    image_url: str = ""           # generated hero/thumbnail image (viewable)
    drafts: dict = field(default_factory=dict)     # Channel -> ChannelDraft
    published: bool = False

    def channel_drafts(self) -> list[ChannelDraft]:
        return [self.drafts[c] for c in self.brief.channels if c in self.drafts]


@dataclass
class ContentResult:
    campaign_id: str
    mission_state: str
    drafts: list[ChannelDraft]
    invariant_ok: bool
    timeline: list = field(default_factory=list)
