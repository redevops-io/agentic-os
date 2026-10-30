"""Crash and retry cannot duplicate or lose a business effect, and cannot bypass approval.

The write boundaries of one action:  insert(accepted) -> [200] -> claim(dispatching) -> audit(RapDispatching) -> gateway(lease, handler -> CRM effect,
lease settled) -> decide(+queue result) -> POST result -> mark delivered.   A hard kill is simulated at each of them."""
import json

import pytest

from conftest import UPDATE, Boom, drive, key_of, payload, restart, settle

SEND = {"contact": "lead_dana01", "body": "Hi Dana, here is the link."}


def test_a_crash_after_acceptance_and_before_the_ack_is_safe(runtime_factory, clock):
    env, srv, rt = runtime_factory()
    code, cid = rt.invoke("update_lead", UPDATE, call_id="call_a")
    assert code == 200
    env2 = restart(env, rt, clock)                                    # died after the commit, the runtime never saw the 200 and asks again
    from redevops_infinity import RapHttpServer
    body = json.dumps({"operation": "update_lead", "arguments": UPDATE, "id": "call_a", "callback_url": f"{rt.cb_origin}/cb/{rt.pending[('thread-root', 'call_a')]['token']}",
                       "group_id": "thread-root"}).encode()
    assert env2.bridge.handle_invoke(env2.peer, body) == (200, "")
    env2.bridge.recover()
    p = settle(env2, rt, "thread-root", "call_a")
    assert p["status"] == "completed" and env.crm.effect_count == 1
    assert len(rt.results[("thread-root", "call_a")]) == 1


@pytest.mark.parametrize("mode", ["before", "after"])
def test_a_crash_at_the_dispatching_record_means_the_effect_has_not_happened_and_recovery_runs_it_once(runtime_factory, clock, fork_support, tmp_path, mode):
    store = fork_support.CrashingStore(str(tmp_path / "rap-journal.jsonl"), mode)
    env, srv, rt = runtime_factory(journal=store)
    from agentic_os.mission.fork_join import SimulatedCrash
    _, cid = rt.invoke("update_lead", UPDATE)                         # journal append #1: RapAccepted
    store.arm(1)                                                      # the next append is RapDispatching
    with pytest.raises(SimulatedCrash):
        env.bridge.tick(inline=True)
    assert env.crm.effect_count == 0 and env.crm.calls == []
    assert env.store.get(key_of(env, "thread-root", cid))["state"] == "dispatching"
    env2 = restart(env, rt, clock)
    assert env2.bridge.recover() == {"redispatch": 1, "completed": 0, "unknown": 0}
    p = settle(env2, rt, "thread-root", cid)
    assert p["status"] == "completed" and env.crm.effect_count == 1
    assert env2.journal.verify().ok


def test_a_crash_after_the_effect_and_before_the_decision_recovers_the_receipt_from_the_ledger(runtime_factory, clock):
    env, srv, rt = runtime_factory()
    _, cid = rt.invoke("update_lead", UPDATE)
    real = env.bridge._decide
    def die(*a, **k): raise Boom()                                     # noqa: E704
    env.bridge._decide = die
    with pytest.raises(Boom):
        env.bridge.tick(inline=True)
    assert env.crm.effect_count == 1                                   # the effect happened; the process died before saying so
    assert env.store.get(key_of(env, "thread-root", cid))["state"] == "dispatching"
    env2 = restart(env, rt, clock)
    assert env2.bridge.recover() == {"redispatch": 0, "completed": 1, "unknown": 0}
    p = settle(env2, rt, "thread-root", cid)
    assert p["status"] == "completed" and p["receipt_ref"].startswith("crm_update_") and env.crm.effect_count == 1


def test_a_crash_inside_the_handler_is_outcome_unknown_and_is_never_sent_again(runtime_factory, clock):
    env, srv, rt = runtime_factory()
    def kill(args, key): raise Boom()                                  # noqa: E704
    env.crm.request_lead_update = kill
    _, cid = rt.invoke("update_lead", UPDATE)
    with pytest.raises(Boom):
        env.bridge.tick(inline=True)
    from conftest import restart as _r
    env2 = _r(env, rt, clock)
    assert env2.bridge.recover() == {"redispatch": 0, "completed": 0, "unknown": 1}
    p = settle(env2, rt, "thread-root", cid)
    assert p["status"] == "outcome_unknown" and p["retry_safe"] is False
    _, c2 = rt.invoke("update_lead", UPDATE)                           # the model asks again: still not sent
    env2.bridge.tick(inline=True)
    p2 = settle(env2, rt, "thread-root", c2)
    assert p2["status"] == "outcome_unknown" and env.crm.effect_count == 0 and env.crm.calls == []


def test_a_reservation_that_never_left_is_run_again_but_one_that_left_is_not(runtime_factory, clock):
    """Drives the ledger states directly: reserved/none -> safe to re-dispatch; dispatched -> outcome_unknown."""
    from agentic_os.mission.authority import Action
    env, srv, rt = runtime_factory()
    _, c1 = rt.invoke("update_lead", UPDATE, call_id="c_reserved")
    _, c2 = rt.invoke("update_lead", {**UPDATE, "status": "nurture"}, call_id="c_left")
    for cid, leave in (("c_reserved", False), ("c_left", True)):
        k = key_of(env, "thread-root", cid)
        assert env.store.cas(k, ("accepted",), "dispatching", corr=f"corr_{cid}")
        dec = env.guard.authorize(f"{env.tenant}:infinity-runtime", env.tenant,
                                  Action("crm.request_lead_update", {"lead_id": "lead_dana01", "status": "qualified"}, 1, True), lease_id=f"corr_{cid}")
        assert dec.allowed
        if leave:
            env.guard.dispatched(f"corr_{cid}")
    env2 = restart(env, rt, clock)
    assert env2.bridge.recover() == {"redispatch": 1, "completed": 0, "unknown": 1}
    drive(env2)
    assert payload(rt.await_result("thread-root", "c_reserved", 3))["status"] == "completed"
    assert payload(rt.await_result("thread-root", "c_left", 3))["status"] == "outcome_unknown"
    assert env.crm.effect_count == 1


def test_a_crash_after_posting_the_result_and_before_marking_it_delivered_delivers_it_once(runtime_factory, clock):
    env, srv, rt = runtime_factory()
    _, cid = rt.invoke("update_lead", UPDATE)
    real = env.store.mark_delivery
    def die(event_id, state, **kw):
        if state == "delivered":
            raise Boom()
        return real(event_id, state, **kw)
    env.store.mark_delivery = die
    with pytest.raises(Boom):
        env.bridge.tick(inline=True)
    assert len(rt.results[("thread-root", cid)]) == 1                  # the POST happened
    env2 = restart(env, rt, clock)
    drive(env2)                                                        # re-delivered with the SAME event id; the runtime drops the duplicate
    assert len(rt.results[("thread-root", cid)]) == 1 and len([w for w in rt.wakes if w[1] == cid]) == 1
    d = env2.store.deliveries_for(key_of(env, "thread-root", cid))
    assert [x["state"] for x in d] == ["delivered"] and d[0]["attempts"] == 1
    assert env.crm.effect_count == 1


def test_a_runtime_that_is_down_gets_the_result_when_it_is_back(runtime_factory, clock):
    env, srv, rt = runtime_factory()
    rt.drop_next_ack = 3
    _, cid = rt.invoke("update_lead", UPDATE)
    for _ in range(12):
        drive(env)
        clock.advance(10)
    p = payload(rt.await_result("thread-root", cid, 2))
    assert p["status"] == "completed" and env.crm.effect_count == 1
    d = env.store.deliveries_for(key_of(env, "thread-root", cid))[0]
    assert d["state"] == "delivered" and d["attempts"] == 4


def test_delivery_stops_when_the_result_expires_or_the_peer_is_revoked(runtime_factory, clock):
    env, srv, rt = runtime_factory(bridge_kwargs={"result_ttl_s": 100})
    rt.drop_next_ack = 99
    _, cid = rt.invoke("lookup_lead", {"lead_id": "lead_dana01"})
    drive(env)
    clock.advance(500)
    drive(env)
    d = env.store.deliveries_for(key_of(env, "thread-root", cid))[0]
    assert d["state"] == "expired"
    env2, srv2, rt2 = runtime_factory(workdir=env.workdir + "/b")
    _, c2 = rt2.invoke("lookup_lead", {"lead_id": "lead_dana01"})
    env2.peers.revoke(env2.peer.key_id)
    drive(env2)
    assert env2.store.deliveries_for(key_of(env2, "thread-root", c2))[0]["state"] == "dropped" and rt2.results == {}


def test_a_crash_cannot_bypass_approval(runtime_factory, clock):
    env, srv, rt = runtime_factory()
    _, cid = rt.invoke("send_followup", SEND)
    drive(env)
    assert env.store.get(key_of(env, "thread-root", cid))["state"] == "awaiting_approval"
    env2 = restart(env, rt, clock)                                     # the in-memory inbox is gone with the process
    env2.bridge.recover()
    for _ in range(4):
        clock.advance(60)
        drive(env2)
    assert env.crm.effect_count == 0 and rt.results == {}              # restart is not approval
    pend = env2.inbox.pending()
    assert len(pend) == 1
    env2.inbox.approve(pend[0]["id"], by="person:ops-lead")
    env2.bridge.resume_awaiting()
    p = settle(env2, rt, "thread-root", cid)
    assert p["status"] == "completed" and env.crm.effect_count == 1
    env3 = restart(env2, rt, clock)                                    # and a restart afterwards does not send it again
    env3.bridge.recover()
    clock.advance(60)
    drive(env3)
    assert env.crm.effect_count == 1 and len(rt.results[("thread-root", cid)]) == 1


def test_the_audit_journal_stays_a_valid_hash_chain_through_all_of_this(runtime_factory):
    env, srv, rt = runtime_factory()
    for i in range(3):
        _, cid = rt.invoke("update_lead", {**UPDATE, "request_ref": f"r{i}"})
        settle(env, rt, "thread-root", cid)
    assert env.journal.verify().ok
    types = {e.type for e in env.journal.all()}
    assert {"RapAccepted", "RapDispatching", "RapDecided", "RapDelivered"} <= types
    blob = json.dumps([e.payload for e in env.journal.all()])
    assert "lead_dana01" not in blob and "qualified" not in blob      # hashes and ids only, never argument values
