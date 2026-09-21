"""Social Intelligence + Governed Social Execution plane (agent-gateway/v1, plan §21-§41).

A provider-neutral extension of the External Agent Gateway: PERMITTED social observations become
evidence-backed :class:`SocialOpportunity` / :class:`MarketSignal` artifacts (Discovery reasoning), and
APPROVED content/engagement become governed ``social.*`` actions with receipts and verification — the
same ActionRequest → Governance → Operator → Receipt → Verification invariant as every other action.

Boundaries this plane keeps (plan §28-§32):
  * observe → qualify → evidence → rank → human review → governed engagement (never scrape → infer → DM);
  * ``commercial_intent`` and ``solution_seeking`` are separate, and UNKNOWN is preserved (never invented);
  * the social adapter carries NO product-specific lead-scoring — reasoning lives in :mod:`.discovery`;
  * provider policy (Reddit/Muse) is authoritative: a capability that is not VERIFIED/POLICY_SCOPED fails
    closed and never falls back to scraping;
  * Learn is strategy-only (reuses ``external.evaluation.assert_strategy_only``).
"""
from .contracts import (
    SOCIAL_CONTRACT_VERSION, ActionClass, ContentDraft, EngagementProposal, MarketSignal,
    ProblemSignal, IntentSignal, Signal, SocialActionReceipt, SocialActionRequest, SocialObservation,
    SocialOpportunity)

__all__ = [
    "SOCIAL_CONTRACT_VERSION", "Signal", "ActionClass",
    "SocialObservation", "ProblemSignal", "IntentSignal", "SocialOpportunity", "MarketSignal",
    "EngagementProposal", "ContentDraft", "SocialActionRequest", "SocialActionReceipt",
]
