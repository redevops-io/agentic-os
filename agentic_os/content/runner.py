"""Run a Content Mission end-to-end over the real Mission Runtime.

Mirrors `RevenueMissionRun`: a `ContentBrief` opens a `content_distribution` mission, the mission runs to
the publish gate (`WAITING_HUMAN`), the owner reviews the concept + previews in the Projects UI and
approves, then the publish step posts to the approved channels. No new runtime — the Mission Runtime owns
lifecycle, the approval gate, saga/undo, events and replay.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from ..mission.executor import Executor
from ..mission.runtime import MissionRuntime
from ..mission.store import EventStore
from ..mission.types import MissionState
from .clients import FakeGenerator, FakePublisher, Generator, Publisher
from .contracts import ContentBrief, ContentCampaign, ContentResult
from .fleet import POLICY_REFS, content_fleet


class ContentMissionRun:
    """A live Content Mission: opened, generated to the publish gate, awaiting the owner's approval."""

    def __init__(self, brief: ContentBrief, *, owner: str = "",
                 generator: Optional[Generator] = None, publisher: Optional[Publisher] = None,
                 store: Optional[EventStore] = None):
        brief.owner = owner or brief.owner
        self.brief = brief
        self.campaign = ContentCampaign(brief=brief)
        reg, client = content_fleet(brief, generator=generator or FakeGenerator(),
                                    publisher=publisher or FakePublisher(), campaign=self.campaign)
        self.rt = MissionRuntime(reg, Executor(client), store=store or EventStore())
        m = self.rt.create_mission(goal=brief.key_message, policy_refs=POLICY_REFS,
                                   template="content_distribution")
        self.mission_id = m.id
        self.rt.run(m.id)                     # runs generation → parks at the publish gate (WAITING_HUMAN)

    @property
    def state(self) -> MissionState:
        return self.rt._missions[self.mission_id].state

    def _pending_node(self) -> Optional[str]:
        p = self.rt.repo.pending_human(self.mission_id)
        return p["node_id"] if p else None

    def preview(self) -> dict:
        """What the owner reviews in the Projects UI before approving: the concept, the rendered reel
        preview URL, and the per-channel drafts produced before the gate."""
        return {
            "campaign_id": self.brief.campaign_id,
            "concept": asdict(self.campaign.concept),
            "reel_preview_url": self.campaign.reel_preview_url,   # presigned, externally viewable
            "reel_store_url": self.campaign.reel_url,
            "image_url": self.campaign.image_url,
            "drafts": [{"channel": d.channel.value, "text": d.text, "media_url": d.media_url,
                        "status": d.status, "post_url": d.post_url} for d in self.campaign.channel_drafts()],
            "channels": [c.value for c in self.brief.channels],
            "awaiting_approval": self.state is MissionState.WAITING_HUMAN,
        }

    def approve(self) -> ContentResult:
        """Owner approves the publish gate → the publish step posts to the approved channels."""
        node = self._pending_node()
        if node is not None:
            self.rt.approve(self.mission_id, node, "approve")
        return self.finalize()

    def reject(self) -> ContentResult:
        node = self._pending_node()
        if node is not None:
            self.rt.approve(self.mission_id, node, "reject")
        return self.finalize()

    def finalize(self) -> ContentResult:
        state = self.state
        return ContentResult(
            campaign_id=self.brief.campaign_id,
            mission_state=state.value if hasattr(state, "value") else str(state),
            drafts=self.campaign.channel_drafts(),
            invariant_ok=state in (MissionState.SUCCEEDED, MissionState.WAITING_HUMAN),
            timeline=self.rt.repo.timeline(self.mission_id))
