"""Seed capability catalog — the audited commercial capabilities as discoverable descriptors.

These are metadata-only descriptors (no bound handler): registering them makes the capabilities *discoverable*
by intent even before each one is wired to a running surface, which is precisely the Phase-0 finding this program
fixes (rich libraries, zero runtime callers). ``status``/``maturity``/``code_ref`` mirror
``SIDEKICK_COMMERCIAL_CAPABILITY_MATRIX.md`` so the registry doubles as a live roadmap: an app binds a handler
(``registry.bind(id, fn)``) as each capability is productised. Descriptors deliberately carry NO import of the
heavy domain impls, so importing the registry stays cheap and provider-neutral.
"""
from __future__ import annotations

from .capability import CapabilityDomain as D, CapabilityRegistry, Maturity as M, SidekickCapability, default_registry

_BUILTINS = (
    SidekickCapability(
        capability_id="acquisition.market_observe", domain=D.ACQUISITION,
        summary="Observe competitor/market acquisition funnels, pages, offers and creatives as evidence.",
        intents=("watch competitors", "what are competitors doing", "track market", "competitor funnel"),
        required_inputs=("watch_list",), optional_inputs=("segment",),
        required_provider_capabilities=("web.fetch",),
        produced_artifacts=("MarketObservations", "MarketPattern", "Opportunity"),
        candidate_actions=("record_observation", "flag_opportunity"),
        verification_contract="content_hash change-detection + bitemporal observed_at/known_at",
        learning_contract="strategy_memory + first-party outcome join",
        maturity=M.L3_MULTI_PROVIDER, status="COMPLETE", code_ref="agentic_os/market/observe.py"),
    SidekickCapability(
        capability_id="acquisition.funnel_optimize", domain=D.ACQUISITION,
        summary="Diagnose and improve a conversion funnel: detect leaks, propose a governed intervention, measure.",
        intents=("improve conversion", "demos aren't converting", "why is conversion dropping",
                 "fix the funnel", "increase signups"),
        required_inputs=("site", "analytics_source"), optional_inputs=("goal", "segment"),
        required_provider_capabilities=("analytics.read", "surface.write"),
        produced_artifacts=("FunnelDiagnosis", "InterventionCandidate"),
        candidate_actions=("CHANGE_COPY", "CHANGE_CTA", "CHANGE_OFFER", "REMOVE_FRICTION", "NO_CHANGE"),
        authority_requirements=("surface.write.approved",),
        verification_contract="governed Executor receipt + first-party Umami/GSC outcome supersede",
        learning_contract="per-site outcome weights re-rank queue",
        maturity=M.L2_GENERALIZED, status="PARTIAL",
        code_ref="agentic_os/content/{search_signals,interventions}.py + growth/loop.py"),
    SidekickCapability(
        capability_id="acquisition.content_create", domain=D.ACQUISITION,
        summary="Produce channel-appropriate content/creative for a commercial objective and publish (governed).",
        intents=("write content", "create a post", "draft a campaign asset", "publish to social"),
        required_inputs=("brief",), optional_inputs=("channels", "goal"),
        required_provider_capabilities=("content.publish",),
        produced_artifacts=("ContentConcept", "ChannelDraft", "PublishOutcome"),
        candidate_actions=("draft", "request_publish_approval"),
        authority_requirements=("content.publish.approved",),
        verification_contract="publish post_id/post_url read-back",
        learning_contract="shared learning picks channel/time/format play, delayed-outcome attribution",
        maturity=M.L2_GENERALIZED, status="COMPLETE", code_ref="agentic_os/content/"),
    SidekickCapability(
        capability_id="sales.deal_state", domain=D.SALES,
        summary="Evidence-backed deal state (reported vs runtime-VERIFIED), distinct from the CRM stage.",
        intents=("what's the real state of this deal", "is this deal actually progressing",
                 "verify the pipeline"),
        required_inputs=("deal_ref",), optional_inputs=("as_of",),
        produced_artifacts=("Deal", "Claim", "DealCondition"),
        verification_contract="reported-vs-verified claims, never auto-reconciled; conservative probability",
        maturity=M.L1_INTERNAL, status="INTERNAL_ONLY", code_ref="agentic_os/deal_closing/contracts.py"),
    SidekickCapability(
        capability_id="sales.deal_close", domain=D.SALES,
        summary="Identify blockers, form a closing hypothesis, score candidate actions, compile a close-plan Mission.",
        intents=("help me close this deal", "what's blocking this deal", "how do I move this forward",
                 "next step to close"),
        required_inputs=("deal_ref",), optional_inputs=("methodology", "autonomy"),
        produced_artifacts=("Blocker", "ClosingHypothesis", "ScoredAction", "DealClosePlan"),
        candidate_actions=("send_security_docs", "schedule_validation", "resolve_quote", "escalate", "WAIT"),
        authority_requirements=("autonomy_level", "approval_gate"),
        verification_contract="executor independent read-back via obligation engine",
        learning_contract="InterventionLedger + calibrate priors",
        maturity=M.L2_GENERALIZED, status="INTERNAL_ONLY",
        code_ref="agentic_os/deal_closing/* + (enterprise) deal_closing/service.py"),
    SidekickCapability(
        capability_id="sales.quote_feasibility", domain=D.SALES,
        summary="Assess whether a requested quote is fulfillable (date/price/margin/inventory) and draft an offer.",
        intents=("can we quote this", "is this order feasible", "quote acme", "can we deliver by",
                 "send a quote"),
        required_inputs=("customer", "line_items"), optional_inputs=("required_by", "payment_terms"),
        required_provider_capabilities=("catalog.read", "inventory.read"),
        produced_artifacts=("QuoteFeasibility", "DraftQuotePlan"),
        candidate_actions=("draft_quote", "offer_substitute", "request_approval"),
        authority_requirements=("discount_authority",),
        verification_contract="deterministic over resolved facts; draft carries required approval",
        maturity=M.L2_GENERALIZED, status="INTERNAL_ONLY", code_ref="agentic_os/revenue/quote.py"),
    SidekickCapability(
        capability_id="commercial.next_action", domain=D.COMMERCIAL,
        summary="The most useful next action for an account/opportunity, evidence-backed — not generic task spam.",
        intents=("what should I do next", "what needs my attention", "next best action", "what matters today"),
        required_inputs=("account_ref",),
        produced_artifacts=("NextAction", "DecisionOpportunity"),
        candidate_actions=("recommend_action", "NO_ACTION"),
        authority_requirements=("risk_tier_gate",),
        learning_contract="shared UtilityModel/bandit + outcome loop",
        maturity=M.L2_GENERALIZED, status="PARTIAL", code_ref="agentic_os/crm_nba.py + priority_engine.py"),
    SidekickCapability(
        capability_id="finance.receivables", domain=D.FINANCE,
        summary="Reason about an overdue receivable using cross-app evidence and propose a governed next action.",
        intents=("chase this invoice", "why is this unpaid", "overdue receivable", "collect payment"),
        required_inputs=("invoice_ref",), optional_inputs=("account_context",),
        required_provider_capabilities=("billing.read", "crm.read"),
        produced_artifacts=("ReceivableCandidate", "FollowupProposal"),
        candidate_actions=("draft_followup", "raise_dispute", "escalate", "WAIT"),
        authority_requirements=("content_digest_approval",),
        verification_contract="execute+verify through integration runner (dunning send)",
        maturity=M.L1_INTERNAL, status="PARTIAL (TEST_ONLY)",
        code_ref="agentic_os/integrations/business/missions.py"),
    SidekickCapability(
        capability_id="acquisition.design_partner_outreach", domain=D.ACQUISITION,
        summary="Run a governed design-partner cold-outreach campaign: find/verify prospects (brokered data "
                "providers), draft personalized emails grounded in verified facts, and stage them for human "
                "approval. Sending stays approval-gated; EU/CAN-SPAM compliance is enforced.",
        intents=("run the design partner campaign", "find prospects and draft outreach",
                 "start cold outreach for design partners", "recruit design partners",
                 "draft outreach emails for these prospects"),
        required_inputs=(), optional_inputs=("prospects", "criteria", "segment", "tenant", "limit"),
        required_provider_capabilities=("data.enrich", "email.send"),
        produced_artifacts=("OutreachDraft", "ApprovalQueue"),
        candidate_actions=("draft_outreach", "request_send_approval", "WAIT"),
        authority_requirements=("outreach.send.approved",),
        verification_contract="reply/bounce read-back → outcome; sends approval-gated; EU/CAN-SPAM gate; "
                              "verified-fact-vs-hypothesis guard on every draft",
        learning_contract="bandit reward on VERIFIED outcomes (reply/meeting/pilot), never opens",
        maturity=M.L2_GENERALIZED, status="COMPLETE",
        code_ref="agentic-os-enterprise apps/revenue-agent/campaign.py (handler bound by the enterprise "
                 "sidekick outreach binding)"),
)


def register_builtin_capabilities(registry: CapabilityRegistry, *, replace: bool = True) -> int:
    """Seed ``registry`` with the audited capability descriptors. Returns the count registered."""
    for cap in _BUILTINS:
        registry.register(cap, replace=replace)
    return len(_BUILTINS)


# Seed the process-wide default registry on import so discovery works out of the box.
register_builtin_capabilities(default_registry)


__all__ = ["register_builtin_capabilities"]
