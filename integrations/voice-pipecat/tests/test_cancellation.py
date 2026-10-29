"""Cancellation and barge-in at the action level: a committed write is never abandoned and never repeated."""
from __future__ import annotations

import asyncio
import threading

from agentic_os_voice.contracts import ActionRequest, ActionStatus, ConfirmationState, Reason
from conftest import UPDATE_ARGS

C = ConfirmationState.CONFIRMED


async def until(pred, timeout=5.0):
    async def wait():
        while not pred():
            await asyncio.sleep(0.002)
    await asyncio.wait_for(wait(), timeout)


async def test_cancelling_the_caller_during_a_write_does_not_cancel_the_write(env, make_session):
    p = make_session(env)
    env.crm.gate = threading.Event()
    task = asyncio.ensure_future(p.call("update_lead", UPDATE_ARGS, confirmation=C))
    await until(env.crm.entered.is_set)                       # the write is inside the CRM
    task.cancel()                                             # a barge-in / framework timeout cancels the handler
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.cancelled()
    env.crm.gate.set()                                        # the CRM finishes
    await env.bridge.drain()
    rec = env.bridge.receipt(next(a for a, r in env.bridge._index.items() if r["tool"] == "update_lead"))
    assert rec["status"] == "completed" and rec["receipt_ref"].startswith("crm_upd_")
    assert env.crm.effect_count == 1
    again = await p.call("update_lead", UPDATE_ARGS, confirmation=C)        # the model asks again after the interruption
    assert again.replayed and again.receipt_ref == rec["receipt_ref"] and env.crm.effect_count == 1


async def test_a_deadline_during_a_write_reports_in_flight_never_completed(env, make_session):
    p = make_session(env)
    env.crm.gate = threading.Event()
    req = ActionRequest("call-1", env.tenant, "update_lead", UPDATE_ARGS, "", (), C)
    r = await env.bridge.submit_async(req, timeout=0.05)
    assert (r.status, r.reason) == (ActionStatus.APPROVED, Reason.IN_FLIGHT) and "cannot say it is done" in r.say
    st = await p.status(r.action_id)
    assert st.status is ActionStatus.APPROVED                  # visible as in flight to the same session
    env.crm.gate.set()
    await env.bridge.drain()
    st = await p.status(r.action_id)
    assert st.status is ActionStatus.COMPLETED and st.receipt_ref.startswith("crm_upd_") and env.crm.effect_count == 1


async def test_a_deadline_during_a_read_is_a_plain_timeout(env, make_session):
    make_session(env)
    gate = threading.Event()
    real = env.crm.lead_lookup
    env.crm.lead_lookup = lambda a: (gate.wait(5), real(a))[1]
    try:
        r = await env.bridge.submit_async(ActionRequest("call-1", env.tenant, "lookup_lead", {"lead_id": "lead_dana01"}), timeout=0.05)
        assert (r.status, r.reason, r.retry_safe) == (ActionStatus.FAILED, Reason.TIMEOUT, True)
    finally:
        gate.set()
        await env.bridge.drain()


def test_a_request_cancelled_before_dispatch_has_no_effect_and_is_journaled(env, make_session):
    make_session(env)
    ev = threading.Event()
    ev.set()
    r = env.bridge.submit(ActionRequest("call-1", env.tenant, "update_lead", UPDATE_ARGS, "", (), C), cancel=ev)
    assert (r.status, r.reason, r.retry_safe) == (ActionStatus.FAILED, Reason.CANCELLED_BEFORE_DISPATCH, True)
    assert env.crm.calls == [] and env.crm.effect_count == 0
    ok = env.bridge.submit(ActionRequest("call-1", env.tenant, "update_lead", UPDATE_ARGS, "", (), C))     # and it can still be asked for
    assert ok.status is ActionStatus.COMPLETED


async def test_cancelling_a_waiting_duplicate_does_not_disturb_the_first_request(env, make_session):
    p = make_session(env)
    env.crm.gate = threading.Event()
    first = asyncio.ensure_future(p.call("update_lead", UPDATE_ARGS, confirmation=C))
    await until(env.crm.entered.is_set)
    dup = asyncio.ensure_future(p.call("update_lead", UPDATE_ARGS, confirmation=C))
    await asyncio.sleep(0.05)
    dup.cancel()
    env.crm.gate.set()
    r = await first
    assert r.status is ActionStatus.COMPLETED and env.crm.effect_count == 1
    await env.bridge.drain()


async def test_no_task_is_left_running_and_no_exception_goes_unretrieved(env, make_session):
    p = make_session(env)
    for _ in range(3):
        await p.call("lookup_lead", {"lead_id": "lead_dana01"})
    await env.bridge.drain()
    assert not env.bridge._tasks
