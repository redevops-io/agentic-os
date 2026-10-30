"""Cancellation, deadlines, late completions, subscriptions and thread closure."""
import json
import threading
import time

from conftest import UPDATE, drive, key_of, payload, settle


def test_cancel_before_dispatch_means_nothing_runs_and_the_runtime_is_told(runtime_factory):
    env, srv, rt = runtime_factory()
    _, cid = rt.invoke("update_lead", UPDATE)
    assert rt.cancel("thread-root", cid) == 200
    p = settle(env, rt, "thread-root", cid)
    assert p["status"] == "cancelled" and p["reason"] == "cancelled_before_dispatch" and env.crm.calls == []


def test_cancel_after_dispatch_never_abandons_a_committed_write(runtime_factory):
    env, srv, rt = runtime_factory()
    gate = threading.Event()
    real = env.crm.request_lead_update
    def slow(args, key):
        gate.wait(5)
        return real(args, key)
    env.crm.request_lead_update = slow
    _, cid = rt.invoke("update_lead", UPDATE)
    t = threading.Thread(target=env.bridge.tick, kwargs={"inline": True})
    t.start()
    for _ in range(100):
        if env.store.get(key_of(env, "thread-root", cid))["state"] == "dispatching":
            break
        time.sleep(0.02)
    assert rt.cancel("thread-root", cid) == 200
    gate.set()
    t.join(5)
    p = settle(env, rt, "thread-root", cid)
    assert p["status"] == "completed" and env.crm.effect_count == 1


def test_a_late_or_repeated_cancel_does_not_mutate_a_finished_action(runtime_factory):
    env, srv, rt = runtime_factory()
    _, cid = rt.invoke("update_lead", UPDATE)
    p = settle(env, rt, "thread-root", cid)
    before = env.store.get(key_of(env, "thread-root", cid))
    for _ in range(3):
        assert rt.cancel("thread-root", cid) == 200
    drive(env)
    after = env.store.get(key_of(env, "thread-root", cid))
    assert (after["state"], after["status"], after["reason"], after["receipt_ref"]) == (before["state"], before["status"], before["reason"], before["receipt_ref"])
    assert len(rt.results[("thread-root", cid)]) == 1 and env.crm.effect_count == 1


def test_cancelling_an_unknown_call_is_a_quiet_200(runtime_factory):
    env, srv, rt = runtime_factory()
    assert rt.cancel("no-such-thread", "no-such-call") == 200
    assert rt.close_thread("no-such-thread") == 200


def test_a_write_that_misses_its_deadline_is_unknown_not_failed_and_the_late_receipt_is_recorded(runtime_factory):
    from redevops_infinity.demo.capabilities import default_operations
    from dataclasses import replace
    ops = default_operations()
    ops["update_lead"] = replace(ops["update_lead"], timeout_s=0.2)
    env, srv, rt = runtime_factory(operations=ops)
    env.crm.slow_s = 0.8
    _, cid = rt.invoke("update_lead", UPDATE)
    p = settle(env, rt, "thread-root", cid)
    assert p["status"] == "outcome_unknown" and p["reason"] == "timeout" and p["retry_safe"] is False
    time.sleep(1.2)                                                       # the handler finished after the deadline
    rec = env.bridge.receipt(env.tenant, p["action_id"])
    assert rec["status"] == "completed" and rec["receipt_ref"].startswith("crm_update_") and env.crm.effect_count == 1
    assert len(rt.results[("thread-root", cid)]) == 1                     # nothing was delivered a second time, nothing was sent again


def test_a_read_that_misses_its_deadline_is_failed_and_safe_to_retry(runtime_factory):
    from redevops_infinity.demo.capabilities import default_operations
    from dataclasses import replace
    ops = default_operations()
    ops["lookup_lead"] = replace(ops["lookup_lead"], timeout_s=0.1)
    env, srv, rt = runtime_factory(operations=ops)
    real = env.crm.lead_lookup
    env.crm.lead_lookup = lambda a: (time.sleep(0.5), real(a))[1]
    _, cid = rt.invoke("lookup_lead", {"lead_id": "lead_dana01"})
    p = settle(env, rt, "thread-root", cid)
    assert p["status"] == "failed" and p["reason"] == "timeout" and p["retry_safe"] is True


def test_a_subscription_streams_status_changes_and_ends_with_a_final_event(runtime_factory, clock):
    env, srv, rt = runtime_factory()
    _, c1 = rt.invoke("send_followup", {"contact": "lead_dana01", "body": "Hi Dana."})
    drive(env)                                                            # parked for approval
    aid = env.store.get(key_of(env, "thread-root", c1))["action_id"]
    _, w = rt.invoke("watch_action", {"action_id": aid}, group_id="thread-root")
    p = settle(env, rt, "thread-root", w)
    assert p["status"] == "completed" and rt.results[("thread-root", w)][0]["subscription"] is True
    env.inbox.approve(env.inbox.pending()[0]["id"], by="person:ops-lead")
    env.bridge.resume_awaiting()
    for _ in range(4):
        drive(env)
        clock.advance(1)
    evs = rt.await_events(1, 5)
    assert evs and all(e["tool_call_id"] == w and e["type"] == "subscription_event" and "id" not in e for e in evs)
    last = json.loads(evs[-1]["text"])
    assert last["status"] == "completed" and last["receipt_ref"].startswith("crm_send_") and evs[-1]["final"] is True
    assert [e for e in evs[:-1] if e.get("final")] == []
    n = len(rt.events)
    clock.advance(30)
    drive(env)
    assert len(rt.events) == n                                            # no event after the final one
    assert env.crm.effect_count == 1


def test_cancelling_a_subscription_stops_its_events(runtime_factory, clock):
    env, srv, rt = runtime_factory()
    _, c1 = rt.invoke("send_followup", {"contact": "lead_dana01", "body": "Hi Dana."})
    drive(env)
    aid = env.store.get(key_of(env, "thread-root", c1))["action_id"]
    _, w = rt.invoke("watch_action", {"action_id": aid})
    settle(env, rt, "thread-root", w)
    assert rt.cancel("thread-root", w) == 200
    env.inbox.approve(env.inbox.pending()[0]["id"], by="person:ops-lead")
    env.bridge.resume_awaiting()
    for _ in range(4):
        drive(env)
        clock.advance(1)
    assert rt.events == [] and env.crm.effect_count == 1


def test_a_watch_on_someone_elses_action_is_refused(runtime_factory):
    env, srv, rt = runtime_factory()
    _, w = rt.invoke("watch_action", {"action_id": "act_" + "0" * 32})
    p = settle(env, rt, "thread-root", w)
    assert p["status"] == "failed" and p["output"] == {"found": False}
    assert env.store.active_subs() == []


def test_closing_a_thread_cancels_its_pending_work(runtime_factory):
    env, srv, rt = runtime_factory()
    _, cid = rt.invoke("send_followup", {"contact": "lead_dana01", "body": "Hi Dana."}, group_id="thread-a")
    drive(env)
    assert rt.close_thread("thread-a") == 200
    p = settle(env, rt, "thread-a", cid)
    assert p["status"] == "cancelled" and env.crm.effect_count == 0
