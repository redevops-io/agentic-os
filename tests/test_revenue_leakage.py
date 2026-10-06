"""Revenue leakage detectors — deterministic §6/§21 rules → Priority-Engine candidates."""
from __future__ import annotations

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.integrations.business.contracts import Opportunity, Provenance
from agentic_os.revenue import leakage as lk

_NOW = 1_800_000_000_000            # fixed clock (ms)
_DAY = 86_400_000


def _opp(stage: str, cents: int, ref: str = "opp1") -> Opportunity:
    return Opportunity(prov=Provenance(provider="twenty", provider_ref=ref, evidence_refs=("ev1",)),
                       name="Acme deal", stage=stage, amount_cents=cents, currency="USD")


def test_stalled_opportunity_fires_when_active_and_stale() -> None:
    r = lk.stalled_opportunity(_opp("negotiation", 2_000_000), last_activity_at_ms=_NOW - 30 * _DAY,
                               has_future_activity=False, now_ms=_NOW, stale_days=14)
    assert r and r.leakage_type is lk.LeakageType.STALLED_OPPORTUNITY
    assert r.expected_value == 0.4 and r.observation_refs == ("ev1",) and 0 < r.urgency <= 1


def test_stalled_abstains_when_terminal_or_fresh_or_future_activity() -> None:
    assert lk.stalled_opportunity(_opp("closed_won", 10), last_activity_at_ms=_NOW - 90 * _DAY,
                                  has_future_activity=False, now_ms=_NOW) is None
    assert lk.stalled_opportunity(_opp("open", 10), last_activity_at_ms=_NOW - 2 * _DAY,
                                  has_future_activity=False, now_ms=_NOW) is None
    assert lk.stalled_opportunity(_opp("open", 10), last_activity_at_ms=_NOW - 90 * _DAY,
                                  has_future_activity=True, now_ms=_NOW) is None


def test_unanswered_quote_intent() -> None:
    assert lk.unanswered_quote_intent("acct:acme", intent="QUOTE_REQUEST", has_open_quote=False)
    assert lk.unanswered_quote_intent("acct:acme", intent="QUOTE_REQUEST", has_open_quote=True) is None
    assert lk.unanswered_quote_intent("acct:acme", intent="GENERAL", has_open_quote=False) is None


def test_quote_followup_gap() -> None:
    assert lk.quote_followup_gap("q1", amount_cents=500000, sent_at_ms=_NOW - 5 * _DAY,
                                 has_followup=False, now_ms=_NOW, gap_days=3)
    assert lk.quote_followup_gap("q1", amount_cents=500000, sent_at_ms=_NOW - 1 * _DAY,
                                 has_followup=False, now_ms=_NOW, gap_days=3) is None
    assert lk.quote_followup_gap("q1", amount_cents=500000, sent_at_ms=_NOW - 5 * _DAY,
                                 has_followup=True, now_ms=_NOW, gap_days=3) is None


def test_resolved_blocker_and_expansion_and_reengagement() -> None:
    assert lk.resolved_blocker_not_acted_on("o1", was_blocked=True, blocker_cleared=True, acted=False)
    assert lk.resolved_blocker_not_acted_on("o1", was_blocked=True, blocker_cleared=True, acted=True) is None
    assert lk.expansion_opportunity("acct", usage_ratio=0.92, account_healthy=True, ratio_threshold=0.8)
    assert lk.expansion_opportunity("acct", usage_ratio=0.92, account_healthy=False) is None
    assert lk.account_reengagement("acct", high_intent_web_activity=True, has_active_opportunity=False,
                                   identity_linked=True)
    # identity not legitimately linked → never fire (§6)
    assert lk.account_reengagement("acct", high_intent_web_activity=True, has_active_opportunity=False,
                                   identity_linked=False) is None


def test_renewal_risk_needs_multiple_factors() -> None:
    assert lk.renewal_risk("acct", factors={"usage_decline"}) is None          # single weak signal → abstain
    r = lk.renewal_risk("acct", factors={"usage_decline", "support_incidents", "renewal_soon"},
                        days_to_renewal=20, amount_cents=3_000_000)
    assert r and r.leakage_type is lk.LeakageType.RENEWAL_RISK and r.urgency == 0.8 and r.confidence >= 0.7


def test_from_leakage_is_consequential_candidate() -> None:
    r = lk.unanswered_quote_intent("acct:acme", intent="QUOTE_REQUEST", has_open_quote=False, amount_cents=1_000_000)
    c = lk.from_leakage(r)
    assert c.risk_tier is RiskTier.CONSEQUENTIAL
    assert c.action_kind == "UNANSWERED_QUOTE_INTENT"
    assert c.required_capabilities == ("erp.quotation.create",)
    assert c.expected_value == r.expected_value and c.confidence == r.confidence


# ── AR aging / dunning (§30 adjacent — post-close receivables) ───────────────────────────────────────
def test_ar_aging_fires_on_overdue_unpaid_no_dunning() -> None:
    r = lk.ar_aging("INV-100", due_at_ms=_NOW - 45 * _DAY, now_ms=_NOW, amount_cents=1_000_000,
                    paid=False, dunning_scheduled=False, observation_refs=("inv_ev",))
    assert r and r.leakage_type is lk.LeakageType.AR_AGING
    assert "31-60" in r.detail                       # 45 days → 31-60 bucket
    assert r.required_capability == "billing.dunning.schedule"
    assert r.observation_refs == ("inv_ev",) and 0 < r.urgency <= 1


def test_ar_aging_abstains_when_paid_or_covered_or_within_grace() -> None:
    base = dict(due_at_ms=_NOW - 45 * _DAY, now_ms=_NOW, amount_cents=1_000_000)
    assert lk.ar_aging("INV-1", paid=True, dunning_scheduled=False, **base) is None
    assert lk.ar_aging("INV-2", paid=False, dunning_scheduled=True, **base) is None   # already being chased
    assert lk.ar_aging("INV-3", due_at_ms=_NOW, now_ms=_NOW, amount_cents=10,
                       paid=False, dunning_scheduled=False) is None                   # not yet past due


def test_ar_aging_older_bucket_is_more_urgent_and_confident() -> None:
    young = lk.ar_aging("INV-y", due_at_ms=_NOW - 10 * _DAY, now_ms=_NOW, amount_cents=500_000,
                        paid=False, dunning_scheduled=False)
    old = lk.ar_aging("INV-o", due_at_ms=_NOW - 120 * _DAY, now_ms=_NOW, amount_cents=500_000,
                      paid=False, dunning_scheduled=False)
    assert young and old
    assert "90+" in old.detail and old.urgency > young.urgency and old.confidence >= young.confidence


def test_ar_aging_lifts_to_consequential_candidate() -> None:
    r = lk.ar_aging("INV-200", due_at_ms=_NOW - 20 * _DAY, now_ms=_NOW, amount_cents=2_000_000,
                    paid=False, dunning_scheduled=False)
    cand = lk.from_leakage(r)
    assert cand.risk_tier is RiskTier.CONSEQUENTIAL and cand.action_kind == "AR_AGING"
