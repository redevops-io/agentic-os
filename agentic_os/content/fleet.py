"""The content capability fleet — the operators a Content Mission dispatches across.

Mirrors `revenue_fleet`: `CapabilitySpec`s the Mission compiler binds to by `provides == step outcome`.
Generation steps (concept/reel/copy) are reversible (not side-effecting) so they don't gate; the single
`content.publish` step is `side_effecting` + `approval_required` — the owner gate that parks the mission
until it's approved in the UI. Handlers close over the campaign + the injected generator/publisher, so
the same specs bind to fakes offline and to the live vibexgen/X/LinkedIn clients in production.
"""
from __future__ import annotations

from ..mission.executor import InMemoryOperatorClient
from ..mission.registry import CapabilityRegistry
from ..mission.types import CapabilityManifest, CapabilitySpec
from .clients import Generator, Publisher
from .contracts import Channel, ChannelDraft, ContentBrief, ContentCampaign

POLICY_REFS = ["content:generate", "content:render", "content:draft", "content:publish", "content:track"]


def content_fleet(brief: ContentBrief, *, generator: Generator, publisher: Publisher,
                  campaign: ContentCampaign) -> tuple[CapabilityRegistry, InMemoryOperatorClient]:
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("vibexgen-studio", [
        # generation is reversible (produces artifacts, posts nothing) → no gate
        CapabilitySpec("content.generate_concept", "vibexgen-studio", provides=["concept_generated"],
                       permissions=["content:generate"], estimated_value="medium"),
        CapabilitySpec("content.render_reel", "vibexgen-studio", provides=["reel_rendered"],
                       permissions=["content:render"], estimated_value="high"),
        CapabilitySpec("content.draft_x", "vibexgen-studio", provides=["x_thread_drafted"],
                       permissions=["content:draft"], estimated_value="low"),
        CapabilitySpec("content.draft_linkedin", "vibexgen-studio", provides=["linkedin_drafted"],
                       permissions=["content:draft"], estimated_value="low"),
    ]))
    reg.register(CapabilityManifest("social-publisher", [
        # the one gate: side-effecting + approval-required → parks WAITING_HUMAN until owner approval
        CapabilitySpec("content.publish", "social-publisher", provides=["content_published"],
                       side_effecting=True, approval_required=True, undo="content.retract",
                       permissions=["content:publish"], estimated_value="high"),
        CapabilitySpec("content.track", "social-publisher", provides=["performance_tracked"],
                       permissions=["content:track"], estimated_value="low"),
    ]))

    def _caption() -> str:
        return f"{campaign.concept.hook} {brief.cta}".strip()

    def _concept(_inputs):
        campaign.concept = generator.generate_concept(brief)
        return {"hook": campaign.concept.hook, "narrative": campaign.concept.narrative,
                "key_points": list(campaign.concept.key_points)}

    def _reel(_inputs):
        r = generator.render_reel(brief, campaign.concept)
        campaign.reel_url = r.get("video_url", "")
        campaign.reel_preview_url = r.get("preview_url", "")
        campaign.reel_task_id = r.get("task_id", "")
        img = generator.generate_image(brief, campaign.concept) if hasattr(generator, "generate_image") else {}
        campaign.image_url = (img or {}).get("image_url", "")
        return {"video_url": campaign.reel_url, "preview_url": campaign.reel_preview_url,
                "image_url": campaign.image_url, "task_id": campaign.reel_task_id, "error": r.get("error", "")}

    def _draft(channel: Channel):
        def handler(_inputs):
            text = generator.draft_text(brief, campaign.concept, channel)
            campaign.drafts[channel] = ChannelDraft(channel, text=text, media_url=campaign.reel_url)
            return {"channel": channel.value, "text": text}
        return handler

    def _publish(_inputs):
        # runs ONLY after the approval gate is resolved. Publish each selected channel; a channel with no
        # configured publisher becomes a manual handoff (never a silent drop).
        results = []
        for ch in brief.channels:
            draft = campaign.drafts.get(ch) or ChannelDraft(
                ch, text=_caption(), media_url=campaign.reel_url if ch.is_video else "")
            if ch.is_video and not draft.media_url:
                draft.media_url = campaign.reel_url
            publisher.publish(draft)
            campaign.drafts[ch] = draft
            results.append({"channel": ch.value, "status": draft.status,
                            "post_url": draft.post_url, "error": draft.error})
        campaign.published = True
        return {"results": results,
                "published": sum(1 for r in results if r["status"] == "published"),
                "manual": sum(1 for r in results if r["status"] == "manual_handoff")}

    handlers = {
        "content.generate_concept": _concept,
        "content.render_reel": _reel,
        "content.draft_x": _draft(Channel.X),
        "content.draft_linkedin": _draft(Channel.LINKEDIN),
        "content.publish": _publish,
        "content.retract": lambda _i: {"retracted": True},
        "content.track": lambda _i: {"tracking": "scheduled",
                                     "channels": [c.value for c in brief.channels]},
    }
    return reg, InMemoryOperatorClient(handlers)
