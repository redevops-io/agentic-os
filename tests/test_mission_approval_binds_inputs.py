"""A human's approval is bound to the inputs they were shown (audit F1): inputs changed between park and
resume invalidate the approval and park the node again instead of running."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher  # noqa: E402
from agentic_os.mission.types import MissionState  # noqa: E402
from tests import test_mission_inputs_delivery_and_explicit_planning as base  # noqa: E402

_op, _rt, _req, seen = base._op, base._rt, base._req, base.seen


def _park(rt, req):
    mid = MissionRuntimeLauncher(rt).launch(req)
    rt.run(mid)
    assert rt._missions[mid].state is MissionState.WAITING_HUMAN
    return mid


def _events(rt, mid, typ):
    return [e for e in rt.store.for_mission(mid) if e.type == typ]


def test_the_parked_packet_carries_a_digest_and_the_facts_the_approver_needs():
    rt = _rt(_op())
    mid = _park(rt, _req(inputs={"account_id": "A1", "amount": 100, "currency": "USD", "to": "jane.doe@acme.com"}))
    parked = _events(rt, mid, "NodeParked")[-1].payload
    assert len(parked["inputs_sha256"]) == 64
    assert parked["action_summary"]["amount"] == 100 and parked["action_summary"]["currency"] == "USD"
    assert "jane.doe@acme.com" not in str(parked)                    # emails are masked in the ledger
    task = parked["human_task"]["evidence"]
    assert any(x.startswith("action=") and "amount=100" in x for x in task)


def test_approval_records_the_digest_the_human_was_shown():
    rt = _rt(_op())
    mid = _park(rt, _req(inputs={"account_id": "A1", "amount": 100}))
    shown = _events(rt, mid, "NodeParked")[-1].payload["inputs_sha256"]
    rt.approve(mid, rt.repo.pending_human(mid)["node_id"], "approve")
    assert _events(rt, mid, "ApprovalGranted")[-1].payload["inputs_sha256"] == shown
    assert rt._missions[mid].state is MissionState.SUCCEEDED         # unchanged inputs run as before


def test_inputs_changed_between_park_and_approve_do_not_run_and_park_again():
    rt = _rt(_op())
    mid = _park(rt, _req(inputs={"account_id": "A1", "amount": 100}))
    rt._missions[mid].inputs["amount"] = 1000                        # tampered after the human's review was prepared
    rt.approve(mid, rt.repo.pending_human(mid)["node_id"], "approve")
    assert seen == []                                                # the handler never ran with $1000
    assert _events(rt, mid, "ApprovalInvalidated")[-1].payload["reason"] == "inputs changed since approval"
    assert rt._missions[mid].state is MissionState.WAITING_HUMAN     # waiting for a NEW decision
    new = _events(rt, mid, "NodeParked")[-1].payload
    assert new["action_summary"]["amount"] == 1000                   # the second review shows the real amount
    rt.approve(mid, rt.repo.pending_human(mid)["node_id"], "approve")
    assert seen and seen[-1][1]["amount"] == 1000                    # runs only after a fresh approval


def test_legacy_approvals_without_a_digest_still_run():
    rt = _rt(_op())
    mid = _park(rt, _req())
    node = rt.repo.pending_human(mid)["node_id"]
    # a pre-digest ApprovalGranted (no inputs_sha256) is grandfathered, as capability-less ones are
    rt.store.append("ApprovalGranted", mid, {"node_id": node, "edit": None,
                                             "capability": rt._capability_of(mid, node)})
    rt.store.append("NodeResumed", mid, {"node_id": node})
    rt.run(mid)
    assert rt._missions[mid].state is MissionState.SUCCEEDED
