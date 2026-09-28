"""Content Missions + Content/Growth Intelligence.

Content Missions: a `ContentBrief` runs through the `content_distribution` template — generate a concept,
render a short-form video + per-channel copy (via the vibexgen.io generator), park at a single human-approval
gate surfaced in the Projects UI, then publish to the approved channels. Instagram/TikTok publish through
vibexgen; X and LinkedIn through their own APIs; a channel with no configured publisher degrades to a
manual handoff. Nothing reaches a live account without the owner's approval.

Content/Growth Intelligence: the deterministic Search-Intelligence signal engine (`search_signals`) that
surfaces where a site can win from Google Search Console observations — the input to the Content Agent's
Search-Intelligence tab and the multi-site content loop.
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
from .search_signals import (  # noqa: F401
    SearchObservation, SearchSignal, SignalType, Thresholds,
    cannibalization, ctr_opportunity, emergent_intent, missing_page, near_win,
)
from .behavior_signals import (  # noqa: F401
    BehaviorSignal, BehaviorSignalType, high_traffic_leverage, scan_behavior, underperforming_page,
)
from .interventions import (  # noqa: F401
    content_intervention_queue, from_content_signal, plan_content_interventions,
)
