"""Revenue Missions — the canonical opportunity contract + the core invariant.

A `RevenueOpportunity` is the one shape every revenue signal (inbound lead, missed call, quote
follow-up, stale lead, referral, government solicitation, …) normalizes into before it enters the
Mission machinery. Writing a CRM record does NOT count as handling the lead — the invariant below is
what "handled" means.

This is the agentic-os (Mission-side) opportunity contract; the geographic discovery/qualification
side lives in `context-runtime` (`integrations/local_gov.py`). They converge on the same fields and
would share a `runtime-contracts` definition once the boundary is frozen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class OpportunityType(str, Enum):
    INBOUND_LEAD = "INBOUND_LEAD"
    MISSED_CALL = "MISSED_CALL"
    QUOTE_FOLLOWUP = "QUOTE_FOLLOWUP"
    REFERRAL = "REFERRAL"
    STALE_LEAD = "STALE_LEAD"
    GOV_SOLICITATION = "GOV_SOLICITATION"
    GOV_FORECAST = "GOV_FORECAST"
    CONTRACT_RECOMPETE = "CONTRACT_RECOMPETE"
    PARTNER_OPPORTUNITY = "PARTNER_OPPORTUNITY"


class Priority(str, Enum):
    """Attention class. P0 act now · P1 today · P2 scheduled · P3 digest."""
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class SendMode(str, Enum):
    """How an outbound customer message is authorized."""
    DRAFT_ONLY = "DRAFT_ONLY"                # agent drafts; owner sends manually (outside the system)
    APPROVE_AND_SEND = "APPROVE_AND_SEND"    # agent drafts; owner approves from a channel; agent sends
    POLICY_AUTO_SEND = "POLICY_AUTO_SEND"    # owner pre-authorized this narrow message class; agent sends


class Disposition(str, Enum):
    OPEN = "OPEN"
    WON = "WON"
    LOST = "LOST"
    DISQUALIFIED = "DISQUALIFIED"
    OPTED_OUT = "OPTED_OUT"


@dataclass
class RevenueOpportunity:
    """A revenue signal normalized to an owned, trackable opportunity."""
    opportunity_id: str
    type: OpportunityType
    source: str                              # e.g. "ai_voice", "web_form", "city_portal"
    summary: str                             # the one-line the owner reads on their phone
    # identity / context
    contact_name: str = ""
    company: str = ""
    requested_service: str = ""
    channel: str = ""                        # the customer channel to reply on (whatsapp/sms/email/…)
    estimated_value: Optional[float] = None
    urgency: str = "normal"                  # "emergency" | "high" | "normal" | "low"
    deadline: str = ""                       # ISO; "" = none
    priority: Priority = Priority.P2
    # ownership + lifecycle (the invariant operates on these)
    owner: str = ""
    crm_record: str = ""                     # external CRM id once created
    stage: str = "new"
    next_action: str = ""                    # a future action …
    waiting_condition: str = ""              # … or an explicit wait …
    disposition: Disposition = Disposition.OPEN   # … or a terminal disposition
    mission_id: str = ""
    evidence_ids: tuple[str, ...] = ()       # REFERENCES to the originating Discovery evidence (not copies)
    proposed_response: str = ""              # the drafted follow-up the owner approves
    # discovery lineage — preserved for discovered (e.g. government) opportunities so the whole chain
    # stays replayable from evidence rather than an opaque summary.
    issuing_entity: str = ""
    department: str = ""                      # issuing department/business unit (from detail enrichment)
    status: str = ""                          # source-declared lifecycle status (e.g. "Posted")
    jurisdiction: str = ""
    source_url: str = ""
    geographic_distance_miles: Optional[float] = None
    qualification_reasons: tuple[str, ...] = ()
    confidence: Optional[float] = None
    discovery_digest: str = ""               # material-content hash for idempotent re-ingest
    # forecast ↔ solicitation linkage (GOV_FORECAST → the formal GOV_SOLICITATION that opens for it)
    correlation_key: str = ""
    linked_opportunity_id: str = ""          # the counterpart (forecast's solicitation, or vice versa)
    forecast_lead_days: Optional[int] = None  # days the forecast preceded the formal solicitation

    def is_active(self) -> bool:
        return self.disposition is Disposition.OPEN


def next_action_invariant(opp: RevenueOpportunity) -> bool:
    """The product invariant: an active qualified opportunity must have an OWNER plus either a future
    next action, an explicit waiting condition, or a terminal disposition. A closed (terminal)
    opportunity satisfies it by its disposition alone."""
    if opp.disposition is not Disposition.OPEN:
        return bool(opp.owner)                      # terminal disposition is a valid end state
    return bool(opp.owner) and bool(opp.next_action or opp.waiting_condition)


def assert_invariant(opp: RevenueOpportunity) -> None:
    """Raise if a live opportunity would silently disappear (no owner / no next state)."""
    if not next_action_invariant(opp):
        raise InvariantViolation(
            f"opportunity {opp.opportunity_id} violates the revenue invariant: "
            f"owner={opp.owner!r}, next_action={opp.next_action!r}, "
            f"waiting={opp.waiting_condition!r}, disposition={opp.disposition.value}")


class InvariantViolation(RuntimeError):
    """A qualified revenue opportunity was left with no owner and no valid next state."""
