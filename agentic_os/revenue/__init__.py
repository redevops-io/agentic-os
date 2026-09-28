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
from .leakage import (  # noqa: F401
    LeakageType, RevenueLeakage, account_reengagement, expansion_opportunity, from_leakage,
    quote_followup_gap, renewal_risk, resolved_blocker_not_acted_on, stalled_opportunity,
    unanswered_quote_intent,
)
from .quote import (  # noqa: F401
    CatalogItem, QuoteFeasibility, QuoteLine, QuoteLineResult, assess_quote_feasibility,
)
from .intent import CommercialIntent, IntentClassification, classify_intent  # noqa: F401
from .flagship import QuotePlan, plan_quote_from_request  # noqa: F401
from .interventions import (  # noqa: F401
    decision_view,
    intervention_queue,
    plan_leakage_interventions,
)
# NOTE: flagship_live is deliberately NOT re-exported here — it imports the integrations clients
# (twenty/erpnext), which import back into revenue.*, so eager import would be circular. Import it by
# module path: `from agentic_os.revenue.flagship_live import plan_quote_from_request_live`.
from .loop import (  # noqa: F401
    ExecutionReceipt, InMemoryExecutor, QuoteExecutor, execute_quote_plan, line_items_from_text,
)
