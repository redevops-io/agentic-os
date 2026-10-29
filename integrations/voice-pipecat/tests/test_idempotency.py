"""Idempotency: a business intent has at most one effect, however many times and ways it is asked for."""
from __future__ import annotations

import asyncio
import threading

from agentic_os_voice.contracts import ActionRequest, ActionStatus, ConfirmationState, IdentityLevel, Reason
from conftest import UPDATE_ARGS

C = ConfirmationState.CONFIRMED


async def update(p, args=None):
    return await p.call("update_lead", args or UPDATE_ARGS, confirmation=C)


async def test_asking_twice_in_a_row_gives_one_effect_and_the_same_receipt(env, make_session):
    p = make_session(env)
    a, b, c = await update(p), await update(p), await update(p)
    assert a.status is ActionStatus.COMPLETED and not a.replayed
    assert b.replayed and c.replayed and {a.receipt_ref, b.receipt_ref, c.receipt_ref} == {a.receipt_ref}
    assert env.crm.effect_count == 1 and len(env.crm.effects_of("lead_update")) == 1
    assert [x["call"] for x in env.crm.calls].count("lead_update") == 1            # the CRM was not even asked a second time


async def test_ten_concurrent_identical_requests_make_one_effect(env, make_session):
    p = make_session(env)
    res = await asyncio.gather(*[update(p) for _ in range(10)])
    assert all(r.status is ActionStatus.COMPLETED for r in res) and len({r.receipt_ref for r in res}) == 1
    assert env.crm.effect_count == 1


def test_concurrent_threads_from_two_sessions_make_one_effect(env, make_session):
    make_session(env, sid="call-1")
    make_session(env, sid="call-2")                                  # the same person calls back: a different session, the same intent
    out, barrier = [], threading.Barrier(8)

    def go(i):
        barrier.wait()
        out.append(env.bridge.submit(ActionRequest(f"call-{1 + i % 2}", env.tenant, "update_lead", UPDATE_ARGS, "", (), C)))
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(r.status is ActionStatus.COMPLETED for r in out) and len({r.receipt_ref for r in out}) == 1
    assert env.crm.effect_count == 1


async def test_a_different_intent_is_a_different_action(env, make_session):
    p = make_session(env)
    a = await update(p)
    b = await update(p, {**UPDATE_ARGS, "timeline": "later"})
    assert a.action_id != b.action_id and a.receipt_ref != b.receipt_ref and env.crm.effect_count == 2


async def test_the_same_key_never_replays_a_result_for_different_arguments(env, make_session):
    """The gateway keys its replay on the idempotency key alone; the bridge derives the key FROM the arguments, so a changed
    argument can never be answered with the old receipt."""
    p = make_session(env)
    a = await update(p)
    b = await update(p, {**UPDATE_ARGS, "budget_band": "over_50k"})
    assert b.receipt_ref != a.receipt_ref and not b.replayed and env.crm.leads["lead_dana01"]["budget_band"] == "over_50k"


async def test_a_caller_supplied_action_id_must_match_the_intent(env, make_session):
    make_session(env)
    r = await env.bridge.submit_async(ActionRequest("call-1", env.tenant, "update_lead", UPDATE_ARGS, "act_0000000000000000", (), C))
    assert r.reason == Reason.INVALID_REQUEST and env.crm.effect_count == 0


async def test_a_duplicate_call_started_webhook_does_not_open_a_second_session_or_effect(env, make_session):
    p1 = make_session(env)
    p2 = make_session(env)                                           # the telephony provider delivered 'call started' twice
    assert (p1.session_id, p2.session_id) == ("call-1", "call-1")
    assert (await update(p1)).status is ActionStatus.COMPLETED and (await update(p2)).replayed
    assert env.crm.effect_count == 1


async def test_a_restart_does_not_forget_a_completed_action(env, make_session, tmp_path, clock):
    from agentic_os_voice.demo.run import build_env
    p = make_session(env)
    a = await update(p)
    env2 = build_env(str(tmp_path), crm=env.crm, clock=clock)         # a new process over the same files
    p2 = make_session(env2)
    b = await update(p2)
    assert b.replayed and b.receipt_ref == a.receipt_ref and env2.crm.effect_count == 1
    assert env2.journal.verify().ok


async def test_reads_are_not_deduplicated_they_have_no_effect(env, make_session):
    p = make_session(env)
    for _ in range(3):
        assert (await p.call("lookup_lead", {"lead_id": "lead_dana01"})).status is ActionStatus.COMPLETED
    assert [x["call"] for x in env.crm.calls].count("lead_lookup") == 3 and env.crm.effect_count == 0
