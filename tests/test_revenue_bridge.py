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
