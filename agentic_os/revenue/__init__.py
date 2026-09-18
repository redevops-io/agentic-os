"""Revenue Missions — turn revenue signals into owned next actions over the Mission Runtime.

A thin specialization of the existing kernel, not a new runtime: a `RevenueOpportunity` normalizes any
signal (inbound lead, missed call, quote follow-up, government solicitation, …); the `inbound_lead`
mission template + revenue capability fleet run it through capture → CRM → draft → owner-approved send
→ log → schedule; the `OwnerAttentionGateway` delivers the one-tap mobile decision on the owner's
channel; the `AutoFollowupPolicy` authorizes narrow message classes for auto-send; and the core
invariant guarantees a qualified opportunity never silently disappears.
"""
from .contracts import (  # noqa: F401
    Disposition, InvariantViolation, OpportunityType, Priority, RevenueOpportunity, SendMode,
    assert_invariant, next_action_invariant,
)
from .policy import AUTO_SENDABLE_CLASSES, AutoFollowupPolicy  # noqa: F401
from .gateway import (  # noqa: F401
    ALLOWED_ACTIONS, AttentionBrief, OwnerAttentionGateway, RecordingChannel, build_brief,
)
from .fleet import revenue_fleet  # noqa: F401
from .runner import RevenueMissionResult, RevenueMissionRun  # noqa: F401
from .sources import (  # noqa: F401
    InMemorySource, RevenueSignal, RevenueSignalSource, SamGovSource, collect, open_missions,
    signal_to_opportunity,
)
from .discovery_bridge import (  # noqa: F401
    HANDOFF_CONTRACT_VERSION, IngestAction, IngestResult, MissionRegistry, QualifiedOpportunity,
    drain_outbox, opportunity_digest, should_open, to_revenue_opportunity,
)
