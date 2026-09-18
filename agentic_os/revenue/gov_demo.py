"""The closed architectural loop, offline: a local-government solicitation → owner-approvable mission.

    local-gov observation → Discovery → geospatial + qualification → PURSUE → revenue-handoff/v1
      → RevenueMission → OwnerAttentionGateway → owner channel → Approve → CRM + mission state → WON

The handoff record below is the real contract context-runtime `local_gov.to_handoff(...)` emits; here we
consume it, open a governed mission, deliver the owner brief, approve it, and show idempotent re-ingest.

Run:  python -m agentic_os.revenue.gov_demo
"""
from __future__ import annotations

import copy

from .contracts import Disposition
from .discovery_bridge import IngestAction, MissionRegistry, QualifiedOpportunity
from .gateway import RecordingChannel
from .policy import AutoFollowupPolicy

HANDOFF = {
    "contract_version": "revenue-handoff/v1", "opportunity_id": "AVE-2026-014", "source": "city_portal",
    "issuing_entity": "City of Aventura", "jurisdiction_id": "fl-aventura",
    "title": "HVAC replacement — community center (12 RTUs)", "summary": "Replace 12 rooftop units.",
    "solicitation_type": "ITB", "place_of_performance_zip": "33180", "geographic_distance_miles": 0.0,
    "categories": ["hvac", "mechanical"], "response_due_at": "2026-10-02", "estimated_value": 180000,
    "source_url": "https://www.cityofaventura.com/bids/AVE-2026-014",
    "evidence_ids": ["ev:ave-014:obs-1", "ev:ave-014:doc-1"], "discovered_at": "2026-09-18T09:00:00Z",
    "qualification": {"decision": "PURSUE", "service_match": 1.0, "distance_miles": 0.0,
                      "reasons": ["service match 100% (hvac, mechanical)",
                                  "issuing jurisdiction (City of Aventura) covers your ZIP",
                                  "response due 2026-10-02"]},
    "geographic_reasoning": ["ZIP 33180 centroid falls inside City of Aventura's boundary"],
    "confidence": 0.95,
}


def main() -> int:
    reg = MissionRegistry(owner="Alex (owner)", channel_factory=lambda: RecordingChannel(name="telegram"))

    # 1. Discovery qualified a real local solicitation as PURSUE → the bridge opens a governed mission.
    res = reg.ingest(QualifiedOpportunity.from_dict(HANDOFF))
    print(f"ingest: {res.action.value} — {res.note}")
    run = res.run
    print(f"opportunity {run.opp.opportunity_id} · evidence refs {list(run.opp.evidence_ids)} · "
          f"deadline {run.opp.deadline} · confidence {run.opp.confidence}")

    # 2. The owner gets the brief on their channel (a gov submission is never auto-sent).
    run.resolve(AutoFollowupPolicy(owner="Alex"), "gov_bid_submission")
    print("\n── Telegram to the owner ──")
    print(run.gateway.channel.sent[-1])

    # 3. Owner approves → mission completes; the whole chain is replayable from events.
    run.owner("APPROVE_AND_SEND")
    result = run.finalize()
    print(f"\nmission: {result.mission_state.value} · invariant {'OK' if result.invariant_ok else 'FAIL'} · "
          f"{len(result.timeline)} events on the replayable timeline")

    # 4. Idempotency: the portal re-lists the same tender → no duplicate lead.
    print(f"\nrediscovered same tender: {reg.ingest(QualifiedOpportunity.from_dict(HANDOFF)).action.value}")
    amended = copy.deepcopy(HANDOFF); amended["response_due_at"] = "2026-09-25"
    a = reg.ingest(QualifiedOpportunity.from_dict(amended))
    print(f"amended deadline: {a.action.value} — same mission {a.run is run} → next: '{a.run.opp.next_action}'")

    # 5. Later, the bid wins.
    run.close(Disposition.WON)
    print(f"\ndisposition: {run.opp.disposition.value}")
    return 0 if result.invariant_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
