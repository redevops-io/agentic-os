"""Human approval for consequential actions, through the gateway's own approvals; and the approval service being down."""
from __future__ import annotations

import pytest

from agentic_os.agent_gateway import ApprovalError
from agentic_os_voice.contracts import ActionStatus, ConfirmationState, Reason
from conftest import SEND_ARGS

C = ConfirmationState.CONFIRMED


async def send(p):
    return await p.call("request_followup_send", SEND_ARGS, confirmation=C)


async def test_a_consequential_action_waits_for_a_named_person_and_then_runs_once(env, make_session):
    p = make_session(env)
    r = await send(p)
    assert (r.status, r.reason) == (ActionStatus.PENDING_APPROVAL, Reason.APPROVAL_REQUIRED) and r.approval_id
    assert env.crm.effects_of("send_outreach") == [] and "not done yet" in r.say
    # the requester is the voice agent's own principal: it cannot approve itself
    with pytest.raises(ApprovalError):
        env.inbox.approve(r.approval_id, by="voice-agent")
    env.inbox.approve(r.approval_id, by="alex.reviewer")
    done = env.bridge.resume(r.action_id)
    assert done.status is ActionStatus.COMPLETED and done.receipt_ref.startswith("msg_")
    assert len(env.crm.effects_of("send_outreach")) == 1
    with pytest.raises(KeyError):
        env.bridge.resume(r.action_id)                                  # nothing left to resume
    again = await send(p)
    assert again.replayed and again.receipt_ref == done.receipt_ref and env.crm.effect_count == 1


async def test_an_approval_is_bound_to_the_exact_request(env, make_session):
    p = make_session(env)
    r = await send(p)
    env.inbox.approve(r.approval_id, by="alex.reviewer")
    other = await p.call("request_followup_send", {**SEND_ARGS, "body": "A different message."}, confirmation=C)
    assert other.status is ActionStatus.PENDING_APPROVAL and other.approval_id != r.approval_id      # approving one never authorises another
    assert env.crm.effects_of("send_outreach") == []


async def test_a_rejected_request_does_not_run(env, make_session):
    p = make_session(env)
    r = await send(p)
    env.inbox.reject(r.approval_id, by="alex.reviewer")
    res = env.bridge.resume(r.action_id)
    assert res.status is ActionStatus.PENDING_APPROVAL and env.crm.effects_of("send_outreach") == []


async def test_an_expired_approval_cannot_be_used(env, make_session, clock):
    p = make_session(env)
    r = await send(p)
    env.inbox.approve(r.approval_id, by="alex.reviewer")
    clock.advance(901)                                                  # past the inbox time limit
    res = env.bridge.resume(r.action_id)
    assert res.status is ActionStatus.PENDING_APPROVAL and env.crm.effects_of("send_outreach") == []


async def test_a_standing_grant_cannot_stand_in_for_the_human(env, make_session):
    """The owner's envelope allows the send to reach the approval gate (tier 2); the policy is MANDATORY, so it still parks."""
    p = make_session(env)
    assert (await send(p)).status is ActionStatus.PENDING_APPROVAL


class DownApprovals:
    pending = staticmethod(lambda: [])

    def is_satisfied(self, request, manifest):
        raise ConnectionError("approval service unavailable")


async def test_approval_service_unavailable_fails_closed_and_recovers(tmp_path, clock, make_session):
    from agentic_os.agent_gateway import InboxApprovalStore
    from agentic_os_voice.demo.run import build_env
    env = build_env(str(tmp_path), inbox=DownApprovals(), clock=clock)
    p = make_session(env)
    r = await send(p)
    assert (r.status, r.reason, r.retry_safe) == (ActionStatus.FAILED, Reason.APPROVAL_UNAVAILABLE, True)
    assert env.crm.effect_count == 0 and env.crm.calls == []
    # service restored: the same request is parked normally, nothing was lost or half-done
    env2 = build_env(str(tmp_path), crm=env.crm, inbox=InboxApprovalStore(clock=clock), clock=clock)
    p2 = make_session(env2)
    assert (await send(p2)).status is ActionStatus.PENDING_APPROVAL
    # the unavailable attempt did not consume or leak authority: no reservation was left counting
    assert env.guard.outstanding("acme-demo:voice-agent") == []


async def test_the_follow_up_draft_is_read_only_and_sends_nothing(env, make_session):
    p = make_session(env)
    r = await p.call("draft_followup", {"contact": "lead_dana01", "goal": "Share the booking link."})
    assert r.status is ActionStatus.COMPLETED and "Hi Dana" in r.output["draft"] and env.crm.effect_count == 0
