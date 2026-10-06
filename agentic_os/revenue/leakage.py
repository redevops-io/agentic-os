"""Revenue leakage detectors (Revenue & Execution Intelligence plan §6, §21).

Deterministic, explainable detectors that scan resolved business state and surface *recoverable* revenue
the org is leaving on the table. Each detector is a pure function returning `Optional[RevenueLeakage]`
(None = abstain, exactly like the Priority Engine's `from_*` adapters), and `from_leakage()` lifts a
detection into a Priority-Engine `InterventionCandidate` so it flows through the existing
decide → approval → mission pipeline. Start deterministic (§21); learned priors only re-weight later.

The cross-system facts a rule needs (an opportunity's last-activity time, a quote's follow-up state, an
account's usage ratio) are NOT on the canonical business objects, so detectors take them as explicit inputs;
a later slice feeds them from the real Twenty / ERPNext / Chatwoot / Lago clients. Consequential by nature —
every candidate is CONSEQUENTIAL and parks on approval.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import InterventionCandidate


class LeakageType(str, Enum):
    STALLED_OPPORTUNITY = "STALLED_OPPORTUNITY"
    UNANSWERED_QUOTE_INTENT = "UNANSWERED_QUOTE_INTENT"
    QUOTE_FOLLOWUP_GAP = "QUOTE_FOLLOWUP_GAP"
    RESOLVED_BLOCKER_NOT_ACTED_ON = "RESOLVED_BLOCKER_NOT_ACTED_ON"
    EXPANSION_OPPORTUNITY = "EXPANSION_OPPORTUNITY"
    ACCOUNT_REENGAGEMENT_SIGNAL = "ACCOUNT_REENGAGEMENT_SIGNAL"
    RENEWAL_RISK = "RENEWAL_RISK"
    AR_AGING = "AR_AGING"


@dataclass(frozen=True)
class RevenueLeakage:
    leakage_type: LeakageType
    subject: str
    expected_value: float          # 0..1 magnitude of recoverable revenue (from amount, normalized)
    confidence: float              # 0..1 probability the leakage is real
    urgency: float                 # 0..1 time sensitivity
    proposed_action: str
    required_capability: str
    detail: str = ""
    amount_cents: int = 0          # raw recoverable amount, for reporting
    observation_refs: Tuple[str, ...] = ()


_VALUE_CAP_CENTS = 5_000_000      # $50k caps the normalized magnitude at 1.0
_TERMINAL_STAGES = {"won", "closed won", "closed_won", "lost", "closed lost", "closed_lost",
                    "closed", "abandoned", "cancelled", "canceled"}
_DAY_MS = 86_400_000


def _value(cents: int) -> float:
    if not cents:
        return 0.3                 # unknown amount → a modest default so it can still be ranked
    return round(min(1.0, max(0, cents) / _VALUE_CAP_CENTS), 4)


def _refs(obj: object) -> Tuple[str, ...]:
    prov = getattr(obj, "prov", None)
    return tuple(getattr(prov, "evidence_refs", ()) or ()) if prov is not None else ()


def stalled_opportunity(opp: object, *, last_activity_at_ms: int, has_future_activity: bool,
                        now_ms: int, stale_days: int = 14) -> Optional[RevenueLeakage]:
    """§6: active Twenty opportunity + no future activity + stale last interaction."""
    stage = (getattr(opp, "stage", "") or "").strip().lower()
    if not stage or stage in _TERMINAL_STAGES or has_future_activity:
        return None
    age_days = (now_ms - last_activity_at_ms) / _DAY_MS
    if age_days <= stale_days:
        return None
    amt = int(getattr(opp, "amount_cents", 0) or 0)
    return RevenueLeakage(
        LeakageType.STALLED_OPPORTUNITY, subject=getattr(opp, "name", "") or "opportunity",
        expected_value=_value(amt), confidence=0.9, urgency=round(min(1.0, 0.4 + 0.02 * (age_days - stale_days)), 3),
        proposed_action=f"Re-engage stalled opportunity (no activity for {int(age_days)}d)",
        required_capability="crm.opportunity.followup", amount_cents=amt,
        detail=f"stage={stage}, last activity {int(age_days)}d ago, no future activity scheduled",
        observation_refs=_refs(opp))


def unanswered_quote_intent(subject: str, *, intent: str, has_open_quote: bool,
                            confidence: float = 0.9, amount_cents: int = 0,
                            observation_refs: Tuple[str, ...] = ()) -> Optional[RevenueLeakage]:
    """§6/§21: Chatwoot QUOTE_REQUEST + no current ERPNext quotation."""
    if intent != "QUOTE_REQUEST" or has_open_quote:
        return None
    return RevenueLeakage(
        LeakageType.UNANSWERED_QUOTE_INTENT, subject=subject, expected_value=_value(amount_cents),
        confidence=confidence, urgency=0.8, proposed_action="Draft a quotation for the requested items",
        required_capability="erp.quotation.create", amount_cents=amount_cents,
        detail="Chatwoot QUOTE_REQUEST with no open ERPNext quotation", observation_refs=observation_refs)


def quote_followup_gap(subject: str, *, amount_cents: int, sent_at_ms: int, has_followup: bool,
                       now_ms: int, gap_days: int = 3,
                       observation_refs: Tuple[str, ...] = ()) -> Optional[RevenueLeakage]:
    """§6/§21: quotation sent + no scheduled CRM follow-up past the gap window."""
    if has_followup:
        return None
    age_days = (now_ms - sent_at_ms) / _DAY_MS
    if age_days <= gap_days:
        return None
    return RevenueLeakage(
        LeakageType.QUOTE_FOLLOWUP_GAP, subject=subject, expected_value=_value(amount_cents),
        confidence=0.9, urgency=round(min(1.0, 0.5 + 0.05 * (age_days - gap_days)), 3),
        proposed_action=f"Schedule a follow-up on the sent quote ({int(age_days)}d, none scheduled)",
        required_capability="crm.followup.schedule", amount_cents=amount_cents,
        detail=f"quote sent {int(age_days)}d ago, no follow-up scheduled", observation_refs=observation_refs)


def resolved_blocker_not_acted_on(subject: str, *, was_blocked: bool, blocker_cleared: bool, acted: bool,
                                  amount_cents: int = 0,
                                  observation_refs: Tuple[str, ...] = ()) -> Optional[RevenueLeakage]:
    """§6: opportunity/order blocked previously + new inventory/supplier state removes the blocker + not acted on."""
    if not (was_blocked and blocker_cleared) or acted:
        return None
    return RevenueLeakage(
        LeakageType.RESOLVED_BLOCKER_NOT_ACTED_ON, subject=subject, expected_value=_value(amount_cents),
        confidence=0.85, urgency=0.7,
        proposed_action="Resume the previously-blocked opportunity/order — the blocker has cleared",
        required_capability="revenue.opportunity.resume", amount_cents=amount_cents,
        detail="was blocked; new inventory/supplier state cleared it; not yet acted on",
        observation_refs=observation_refs)


def expansion_opportunity(subject: str, *, usage_ratio: float, account_healthy: bool,
                          ratio_threshold: float = 0.8, amount_cents: int = 0,
                          observation_refs: Tuple[str, ...] = ()) -> Optional[RevenueLeakage]:
    """§6/§21: Lago usage approaches/exceeds the plan boundary on a healthy account."""
    if not account_healthy or usage_ratio < ratio_threshold:
        return None
    headroom = max(1e-6, 1.0 - ratio_threshold)
    return RevenueLeakage(
        LeakageType.EXPANSION_OPPORTUNITY, subject=subject, expected_value=_value(amount_cents),
        confidence=round(min(0.95, 0.6 + 0.4 * (usage_ratio - ratio_threshold) / headroom), 3), urgency=0.5,
        proposed_action=f"Propose a plan expansion (usage at {usage_ratio:.0%} of plan)",
        required_capability="billing.expansion.propose", amount_cents=amount_cents,
        detail=f"usage {usage_ratio:.0%} >= {ratio_threshold:.0%} threshold on a healthy account",
        observation_refs=observation_refs)


def account_reengagement(subject: str, *, high_intent_web_activity: bool, has_active_opportunity: bool,
                         identity_linked: bool,
                         observation_refs: Tuple[str, ...] = ()) -> Optional[RevenueLeakage]:
    """§6: known account shows new high-intent Umami behavior + no active opportunity. ONLY where the
    web→account identity linkage is legitimate (identity_linked)."""
    if not (high_intent_web_activity and identity_linked) or has_active_opportunity:
        return None
    return RevenueLeakage(
        LeakageType.ACCOUNT_REENGAGEMENT_SIGNAL, subject=subject, expected_value=0.4, confidence=0.7, urgency=0.55,
        proposed_action="Re-engage the account showing new high-intent activity",
        required_capability="crm.opportunity.create",
        detail="known account, new high-intent web activity, no active opportunity", observation_refs=observation_refs)


_RENEWAL_FACTORS = {"renewal_soon", "support_incidents", "usage_decline", "billing_friction", "unresolved_blocker"}


def renewal_risk(subject: str, *, factors, min_factors: int = 2, days_to_renewal: Optional[int] = None,
                 amount_cents: int = 0, observation_refs: Tuple[str, ...] = ()) -> Optional[RevenueLeakage]:
    """§6: combine renewal timing + support incidents + usage decline + billing friction + unresolved
    blockers. NEVER infer churn from one weak signal — requires >= min_factors independent factors."""
    present = {f for f in factors if f in _RENEWAL_FACTORS}
    if len(present) < min_factors:
        return None
    return RevenueLeakage(
        LeakageType.RENEWAL_RISK, subject=subject, expected_value=_value(amount_cents),
        confidence=round(min(0.9, 0.4 + 0.15 * len(present)), 3),
        urgency=0.8 if (days_to_renewal is not None and days_to_renewal <= 30) else 0.6,
        proposed_action="Open a renewal-risk mitigation (multiple churn factors present)",
        required_capability="revenue.renewal.mitigate", amount_cents=amount_cents,
        detail="factors: " + ", ".join(sorted(present)), observation_refs=observation_refs)


def _aging_band(days: float) -> str:
    """Standard AR aging buckets."""
    if days <= 30:
        return "1-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def ar_aging(subject: str, *, due_at_ms: int, now_ms: int, amount_cents: int, paid: bool,
             dunning_scheduled: bool, grace_days: int = 1,
             observation_refs: Tuple[str, ...] = ()) -> Optional[RevenueLeakage]:
    """§30 adjacent opportunity (post-close receivables): an invoice that is past due, unpaid, and has no
    dunning / collection step scheduled is recoverable revenue leaking. Abstains on a paid invoice, one not
    yet past the grace window, or one a collection step already covers — so it never double-duns. Urgency and
    confidence rise with the aging bucket; a hard ledger fact, so confidence starts high."""
    if paid or dunning_scheduled:
        return None
    age_days = (now_ms - due_at_ms) / _DAY_MS
    if age_days <= grace_days:
        return None
    band = _aging_band(age_days)
    # older buckets are both more urgent and (empirically) less collectable but more clearly a real leak
    urgency = round(min(1.0, 0.4 + 0.2 * ("1-30 31-60 61-90 90+".split().index(band))), 3)
    return RevenueLeakage(
        LeakageType.AR_AGING, subject=subject, expected_value=_value(amount_cents),
        confidence=round(min(0.95, 0.8 + 0.05 * "1-30 31-60 61-90 90+".split().index(band)), 3),
        urgency=urgency,
        proposed_action=f"Initiate collection / dunning on the overdue invoice ({int(age_days)}d, {band} bucket)",
        required_capability="billing.dunning.schedule", amount_cents=amount_cents,
        detail=f"invoice {int(age_days)}d overdue ({band} aging bucket), unpaid, no dunning scheduled",
        observation_refs=observation_refs)


def from_leakage(leak: RevenueLeakage, *, source_app: str = "revenue") -> InterventionCandidate:
    """Lift a detected leakage into a Priority-Engine candidate. Recovering leaked revenue means an
    outbound / state-changing action, so it is CONSEQUENTIAL and parks on approval (§17)."""
    return InterventionCandidate(
        source_app=source_app, subject=leak.subject, proposed_action=leak.proposed_action,
        expected_value=leak.expected_value, confidence=leak.confidence, urgency=leak.urgency,
        execution_cost=0.15, attention_cost=0.25, risk_tier=RiskTier.CONSEQUENTIAL, reversibility=0.5,
        required_capabilities=(leak.required_capability,), observation_refs=leak.observation_refs,
        candidate_id=f"revenue:{leak.leakage_type.value}:{leak.subject}", action_kind=leak.leakage_type.value)
