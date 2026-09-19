"""Restart/replay across the Mission boundary — the durable-persistence guarantee.

A fresh MissionRegistry process, pointed at the same durable index + mission event store, must:
  * reload what it already knows (no duplicate lead on rediscovery — UNCHANGED across the restart),
  * detect a real amendment as AMENDED, and
  * rehydrate the parked mission by EXACT REPLAY so the owner resumes the same decision and completes it.

Together with the collector+qualification restart/replay on the context-runtime side, this closes the
loop: the whole chain survives a process restart deterministically.
"""
from __future__ import annotations

import copy

from agentic_os.mission.types import MissionState
from agentic_os.revenue import (
    AutoFollowupPolicy, IngestAction, MissionRegistry, QualifiedOpportunity, RecordingChannel,
)

HANDOFF = {
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
    "qualification": {"decision": "PURSUE", "reasons": ["service match 100% (engineering)"],
                      "service_match": 1.0, "distance_miles": 0.0},
    "geographic_reasoning": ["ZIP 33180 is in Miami-Dade County"], "confidence": 0.95,
}


def _registry(tmp_path):
    return MissionRegistry(owner="Alex", channel_factory=lambda: RecordingChannel(name="telegram"),
                           persist_path=str(tmp_path / "registry.jsonl"),
                           mission_store_dir=str(tmp_path / "missions"))


def test_restart_rediscovery_is_unchanged_no_duplicate_lead(tmp_path):
    reg1 = _registry(tmp_path)
    first = reg1.ingest(QualifiedOpportunity.from_dict(HANDOFF))
    assert first.action is IngestAction.CREATED
    mid = first.run.mission_id

    # ── restart: a brand-new registry reloads the durable index ──
    reg2 = _registry(tmp_path)
    again = reg2.ingest(QualifiedOpportunity.from_dict(HANDOFF))
    assert again.action is IngestAction.UNCHANGED          # rediscovered across restart → no duplicate
    assert again.run is not None and again.run.mission_id == mid   # SAME mission, rehydrated from the log
    assert again.run.state is MissionState.WAITING_HUMAN   # parked at the owner gate, exactly as left


def test_restart_then_owner_completes_the_rehydrated_mission(tmp_path):
    reg1 = _registry(tmp_path)
    reg1.ingest(QualifiedOpportunity.from_dict(HANDOFF))

    # owner never acted before the restart; a fresh process rehydrates and the owner finishes the decision
    reg2 = _registry(tmp_path)
    res = reg2.ingest(QualifiedOpportunity.from_dict(HANDOFF))
    run = res.run
    run.resolve(AutoFollowupPolicy(owner="Alex"), "gov_bid_submission")
    assert run.gateway.channel.sent and "Miami-Dade County" in run.gateway.channel.sent[-1]
    run.owner("APPROVE_AND_SEND")
    assert run.state is MissionState.SUCCEEDED
    result = run.finalize()
    assert result.invariant_ok
    # lineage survived the restart intact (references, not copies)
    assert run.opp.evidence_ids == ("obs:src-miami-dade-informs:list:fc9",
                                    "obs:src-miami-dade-informs:detail:0d4")


def test_restart_detects_amendment(tmp_path):
    reg1 = _registry(tmp_path)
    reg1.ingest(QualifiedOpportunity.from_dict(HANDOFF))

    reg2 = _registry(tmp_path)
    amended = copy.deepcopy(HANDOFF)
    amended["response_due_at"] = "2026-09-14"              # deadline moved up (material change)
    amended["status"] = "Posted"
    res = reg2.ingest(QualifiedOpportunity.from_dict(amended))
    assert res.action is IngestAction.AMENDED             # recognized as the SAME tender, amended
    assert res.run is not None and res.run.opp.deadline == "2026-09-14"


def test_drain_outbox_is_idempotent(tmp_path):
    # the soak (collection side) writes handoffs to a JSONL outbox; the Mission side drains it
    from agentic_os.revenue import drain_outbox
    import json
    outbox = tmp_path / "handoffs.jsonl"
    outbox.write_text(json.dumps(HANDOFF) + "\n")

    reg = _registry(tmp_path)
    first = drain_outbox(str(outbox), reg)
    assert first.get("CREATED") == 1                      # opened one mission

    # re-draining the SAME outbox (e.g. next scheduled tick) opens no duplicate — idempotent seam
    reg2 = _registry(tmp_path)
    again = drain_outbox(str(outbox), reg2)
    assert again.get("UNCHANGED") == 1 and "CREATED" not in again
