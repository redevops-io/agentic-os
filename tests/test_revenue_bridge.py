"""Discovery → Revenue Mission bridge: the closed loop + evidence lineage + idempotency.

The HANDOFF fixture below is the REAL output of context-runtime `local_gov.to_handoff(...)` for a
qualified local-government solicitation (ZIP 33180 · City of Aventura) — the boring revenue-handoff/v1
contract the two repos agree on. Copying the real emit output here keeps the consumer test honest and
the two sides in sync.
"""
from __future__ import annotations

import copy

from agentic_os.mission.types import MissionState
from agentic_os.revenue import (
    AutoFollowupPolicy, Disposition, IngestAction, MissionRegistry, OpportunityType,
    QualifiedOpportunity, RecordingChannel, to_revenue_opportunity,
)

# ── the real revenue-handoff/v1 record emitted by context-runtime local_gov.to_handoff ──
HANDOFF = {
    "contract_version": "revenue-handoff/v1",
    "opportunity_id": "AVE-2026-014",
    "source": "city_portal",
    "issuing_entity": "City of Aventura",
    "jurisdiction_id": "fl-aventura",
    "title": "HVAC replacement — community center (12 RTUs)",
    "summary": "Replace 12 rooftop units.",
    "solicitation_type": "ITB",
    "place_of_performance_zip": "33180",
    "geographic_distance_miles": 0.0,
    "categories": ["hvac", "mechanical"],
    "posted_at": "",
    "response_due_at": "2026-10-02",
    "estimated_value": 180000,
    "required_licenses": [],
    "required_certifications": [],
    "source_url": "https://www.cityofaventura.com/bids/AVE-2026-014",
    "evidence_ids": ["ev:ave-014:obs-1", "ev:ave-014:doc-1"],
    "discovered_at": "2026-09-18T09:00:00Z",
    "qualification": {
        "decision": "PURSUE",
        "reasons": ["service match 100% (hvac, mechanical)",
                    "issuing jurisdiction (City of Aventura) covers your ZIP",
                    "response due 2026-10-02"],
        "service_match": 1.0,
        "distance_miles": 0.0,
    },
    "geographic_reasoning": ["ZIP 33180 centroid falls inside City of Aventura's boundary"],
    "confidence": 0.95,
}


def test_bridge_preserves_lineage_as_references():
    qo = QualifiedOpportunity.from_dict(HANDOFF)
    opp = to_revenue_opportunity(qo, owner="Alex")
    assert opp.type is OpportunityType.GOV_SOLICITATION
    assert opp.opportunity_id == "AVE-2026-014"
    assert opp.evidence_ids == ("ev:ave-014:obs-1", "ev:ave-014:doc-1")   # references, not copies
    assert opp.source_url.startswith("https://") and opp.issuing_entity == "City of Aventura"
    assert opp.deadline == "2026-10-02" and opp.confidence == 0.95
    # qualification + geographic reasoning are carried onto the mission opportunity
    assert any("service match" in r for r in opp.qualification_reasons)
    assert any("boundary" in r for r in opp.qualification_reasons)


def test_closed_loop_gov_solicitation_to_won():
    reg = MissionRegistry(owner="Alex", channel_factory=lambda: RecordingChannel(name="telegram"))
    res = reg.ingest(QualifiedOpportunity.from_dict(HANDOFF))
    assert res.action is IngestAction.CREATED
    run = res.run
    assert run.state is MissionState.WAITING_HUMAN                          # parked at the send gate

    # a government solicitation follow-up is never auto-sent → owner approves on their channel
    run.resolve(AutoFollowupPolicy(owner="Alex"), "gov_bid_submission")
    assert run.gateway.channel.sent and "City of Aventura" in run.gateway.channel.sent[-1]
    run.owner("APPROVE_AND_SEND")
    assert run.state is MissionState.SUCCEEDED
    result = run.finalize()
    assert result.invariant_ok
    assert run.opp.evidence_ids == ("ev:ave-014:obs-1", "ev:ave-014:doc-1")  # lineage intact end to end
    assert result.timeline                                                    # replayable from events

    won = run.close(Disposition.WON)
    assert won.opportunity.disposition is Disposition.WON


def test_idempotent_rediscovery_is_a_noop():
    reg = MissionRegistry(owner="Alex", channel_factory=RecordingChannel)
    first = reg.ingest(QualifiedOpportunity.from_dict(HANDOFF))
    again = reg.ingest(QualifiedOpportunity.from_dict(HANDOFF))            # same tender, rediscovered
    assert first.action is IngestAction.CREATED
    assert again.action is IngestAction.UNCHANGED
    assert again.run is first.run                                         # SAME mission, no duplicate lead


def test_amendment_reawakens_the_same_mission():
    reg = MissionRegistry(owner="Alex", channel_factory=lambda: RecordingChannel(name="telegram"))
    first = reg.ingest(QualifiedOpportunity.from_dict(HANDOFF))
    briefs_before = len(first.run.gateway.channel.sent)

    amended = copy.deepcopy(HANDOFF)
    amended["response_due_at"] = "2026-09-25"                             # deadline moved up (material)
    amended["discovered_at"] = "2026-09-20T09:00:00Z"
    res = reg.ingest(QualifiedOpportunity.from_dict(amended))
    assert res.action is IngestAction.AMENDED
    assert res.run is first.run                                           # same mission, not a new lead
    assert res.run.opp.deadline == "2026-09-25" and res.run.opp.next_action.startswith("amended")
    # owner still holds the decision → an updated brief was re-delivered
    assert len(first.run.gateway.channel.sent) > briefs_before
    assert "[amended]" in first.run.gateway.channel.sent[-1]


def test_plain_review_or_reject_is_skipped():
    import copy as _c
    d = _c.deepcopy(HANDOFF)
    d["qualification"]["decision"] = "REVIEW"
    d["response_due_at"] = ""                                             # no deadline → not urgent
    reg = MissionRegistry(owner="Alex")
    assert reg.ingest(QualifiedOpportunity.from_dict(d)).action is IngestAction.SKIPPED
    d["qualification"]["decision"] = "REJECT"
    assert reg.ingest(QualifiedOpportunity.from_dict(d)).action is IngestAction.SKIPPED


# ── GOV_FORECAST (source #2) + deadline-driven priority + forecast↔solicitation linkage ──
def _forecast_handoff() -> dict:
    return {
        "contract_version": "revenue-handoff/v1",
        "opportunity_id": "src-miami-dade-future:FUT-8960",
        "opportunity_kind": "gov_forecast", "correlation_key": "fl-miami-dade-county:bond-engineering",
        "source": "src-miami-dade-future", "issuing_entity": "Miami-Dade County",
        "jurisdiction_id": "fl-miami-dade-county", "title": "Bond Engineering Services",
        "summary": "Bond Engineering Services", "solicitation_type": "FORECAST", "status": "",
        "categories": ["engineering"], "posted_at": "2026-08-01", "response_due_at": "2026-10-30",
        "source_url": "https://www.miamidade.gov/apps/ISD/stratproc/",
        "evidence_ids": ["obs:src-miami-dade-future:list:aaa"], "discovered_at": "2026-08-01T09:00:00Z",
        "qualification": {"decision": "PURSUE", "reasons": ["service match 100% (engineering)"],
                          "service_match": 1.0, "distance_miles": 0.0},
        "geographic_reasoning": ["ZIP 33180 is in Miami-Dade County"], "confidence": 0.9,
    }


def _solicitation_handoff() -> dict:
    """The formal INFORMS solicitation that later opens for the forecast above (same correlation_key)."""
    return {
        "contract_version": "revenue-handoff/v1",
        "opportunity_id": "src-miami-dade-informs:E26SP01",
        "opportunity_kind": "gov_solicitation", "correlation_key": "fl-miami-dade-county:bond-engineering",
        "source": "src-miami-dade-informs", "issuing_entity": "Miami-Dade County",
        "department": "Strategic Procurement", "jurisdiction_id": "fl-miami-dade-county",
        "title": "E26SP01: Bond Engineering Services", "summary": "Bond Engineering Services",
        "solicitation_type": "RFP", "status": "Posted", "categories": ["engineering"],
        "posted_at": "2026-09-15", "response_due_at": "2026-09-21", "detail_observed": True,
        "source_url": "https://supplier.miamidade.gov/...",
        "evidence_ids": ["obs:src-miami-dade-informs:list:fc9", "obs:src-miami-dade-informs:detail:0d4"],
        "discovered_at": "2026-09-15T09:00:00Z",
        "qualification": {"decision": "PURSUE", "reasons": ["service match 100% (engineering)",
                          "issuing jurisdiction (Miami-Dade County) covers your ZIP", "response due 2026-09-21"],
                          "service_match": 1.0, "distance_miles": 0.0},
        "geographic_reasoning": ["ZIP 33180 is in Miami-Dade County"], "confidence": 0.95,
    }


def test_forecast_maps_to_gov_forecast_type_and_is_not_urgent():
    from agentic_os.revenue import OpportunityType, Priority
    qo = QualifiedOpportunity.from_dict(_forecast_handoff())
    opp = to_revenue_opportunity(qo, owner="Alex")
    assert opp.type is OpportunityType.GOV_FORECAST          # distinct type, not GOV_SOLICITATION
    assert opp.urgency == "normal" and opp.priority is not Priority.P0   # a forecast is never act-now
    assert opp.summary.startswith("[forecast]") and opp.correlation_key


def test_priority_is_driven_by_the_real_deadline():
    from agentic_os.revenue import Priority
    from agentic_os.revenue.discovery_bridge import _priority
    today = "2026-09-18"
    near = QualifiedOpportunity.from_dict({**_solicitation_handoff(), "response_due_at": "2026-09-20"})
    soon = QualifiedOpportunity.from_dict({**_solicitation_handoff(), "response_due_at": "2026-09-28"})
    far = QualifiedOpportunity.from_dict({**_solicitation_handoff(), "response_due_at": "2026-11-30"})
    assert _priority(near, today=today) is Priority.P0       # ≤3 days → act now (not a flat P2 fallback)
    assert _priority(soon, today=today) is Priority.P1       # ≤14 days → today
    assert _priority(far, today=today) is Priority.P2        # dated but distant → scheduled


def test_forecast_links_to_the_solicitation_that_opens_for_it():
    reg = MissionRegistry(owner="Alex", channel_factory=RecordingChannel)
    f = reg.ingest(QualifiedOpportunity.from_dict(_forecast_handoff()))
    s = reg.ingest(QualifiedOpportunity.from_dict(_solicitation_handoff()))
    assert f.action is IngestAction.CREATED and s.action is IngestAction.CREATED
    link = reg.linkage("src-miami-dade-informs:E26SP01")
    assert link is not None
    assert link["linked_opportunity_id"] == "src-miami-dade-future:FUT-8960"
    # forecast posted 2026-08-01, formal solicitation posted 2026-09-15 → 45 days of lead
    assert link["forecast_lead_days"] == 45
