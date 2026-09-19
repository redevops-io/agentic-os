"""Content Missions — content creation + multi-channel distribution as a governed Mission.

A `ContentBrief` runs through the `content_distribution` template: generate a concept, render a
short-form video + per-channel copy (via the vibexgen.io generator), park at a single human-approval
gate surfaced in the Projects UI, then publish to the approved channels. Instagram/TikTok publish through
vibexgen; X and LinkedIn through their own APIs; a channel with no configured publisher degrades to a
manual handoff. Nothing reaches a live account without the owner's approval.
"""
from .contracts import (  # noqa: F401
    Channel, ChannelDraft, ContentBrief, ContentCampaign, ContentConcept, ContentResult, PublishOutcome,
)
from .clients import (  # noqa: F401
    FakeGenerator, FakePublisher, Generator, LinkedInPublisher, MultiPublisher, NoOpPublisher, Publisher,
    VibexgenGenerator, VibexgenPublisher, XPublisher,
)
from .fleet import POLICY_REFS, content_fleet  # noqa: F401
from .runner import ContentMissionRun  # noqa: F401
from .projection import ContentMissionRegistry, ContentProjectionProvider  # noqa: F401
