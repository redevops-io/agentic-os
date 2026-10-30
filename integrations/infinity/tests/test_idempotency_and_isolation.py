"""Duplicate invocations, model retries, id reuse, and tenant / principal isolation."""
import json

from conftest import UPDATE, drive, payload, settle
from redevops_infinity.callback_auth import Peer
from redevops_infinity.demo.env import AUDIENCE, SECRET


def test_a_duplicate_invocation_is_acknowledged_and_executed_once(runtime_factory):
    env, srv, rt = runtime_factory()
    code, cid = rt.invoke("update_lead", UPDATE, call_id="call_dup")
    assert code == 200
    assert rt.invoke("update_lead", UPDATE, call_id="call_dup")[0] == 200          # the runtime retried (lost ack): same id, same arguments
    p = settle(env, rt, "thread-root", cid)
    assert p["status"] == "completed" and env.crm.effect_count == 1
    assert len(rt.results[("thread-root", cid)]) == 1


def test_the_same_id_with_different_arguments_is_refused(runtime_factory):
    env, srv, rt = runtime_factory()
    assert rt.invoke("update_lead", UPDATE, call_id="call_x")[0] == 200
    code, _ = rt.invoke("update_lead", {**UPDATE, "status": "nurture"}, call_id="call_x")
    assert code == 409
    drive(env)
    assert env.crm.effects.keys() and env.crm.effect_count == 1


def test_a_model_retry_with_a_new_call_id_does_not_repeat_the_effect(runtime_factory):
    env, srv, rt = runtime_factory()
    _, c1 = rt.invoke("update_lead", UPDATE)
    p1 = settle(env, rt, "thread-root", c1)
    _, c2 = rt.invoke("update_lead", UPDATE)                                      # the model asked again under a new tool-call id
    p2 = settle(env, rt, "thread-root", c2)
    assert p1["action_id"] == p2["action_id"] and p2["status"] == "completed" and p2["reason"] == "replayed"
    assert p2["receipt_ref"] == p1["receipt_ref"] and env.crm.effect_count == 1


def test_a_deliberate_repeat_uses_a_new_request_ref(runtime_factory):
    env, srv, rt = runtime_factory()
    _, c1 = rt.invoke("update_lead", {**UPDATE, "request_ref": "r1"})
    _, c2 = rt.invoke("update_lead", {**UPDATE, "request_ref": "r2"})
    settle(env, rt, "thread-root", c1)
    settle(env, rt, "thread-root", c2)
    assert env.crm.effect_count == 2


def test_the_tenant_comes_from_the_credential_not_from_the_body(runtime_factory, clock):
    env, srv, rt = runtime_factory()
    code, cid = rt.invoke("lookup_lead", {"lead_id": "lead_dana01"}, raw={
        "operation": "lookup_lead", "arguments": {"lead_id": "lead_dana01"}, "id": "c9", "callback_url": f"{rt.cb_origin}/cb/t", "group_id": "g",
        "tenant": "another-tenant"})
    assert code == 400                                                              # unknown field: a body cannot name a tenant
    rt.tokens["t"] = ("g", "c9")


def test_two_tenants_with_the_same_thread_and_call_ids_do_not_collide(runtime_factory, clock):
    env, srv, rt = runtime_factory()
    other = env.peers.add(Peer("key-2", b"o" * 32, "other-tenant", "other-runtime", AUDIENCE, callback_origins=env.peer.callback_origins))
    row_a = {"op": "lookup_lead"}
    _, cid = rt.invoke("lookup_lead", {"lead_id": "lead_dana01"}, call_id="shared", group_id="shared-thread")
    from redevops_infinity.store import rec_key
    assert env.store.get(rec_key("acme-demo", "shared-thread", "shared")) is not None
    assert env.store.get(rec_key("other-tenant", "shared-thread", "shared")) is None
    # a different tenant's peer cannot cancel or close the first tenant's call: it addresses its own (empty) namespace
    env.bridge.handle_cancel(other, json.dumps({"thread_id": "shared-thread", "tool_call_id": "shared"}).encode())
    env.bridge.handle_close_thread(other, json.dumps({"thread_id": "shared-thread"}).encode())
    assert env.store.get(rec_key("acme-demo", "shared-thread", "shared"))["state"] == "accepted"
    settle(env, rt, "shared-thread", "shared")


def test_another_principal_of_the_same_tenant_cannot_cancel_a_call(runtime_factory):
    env, srv, rt = runtime_factory()
    sibling = env.peers.add(Peer("key-3", b"q" * 32, env.tenant, "sibling-runtime", AUDIENCE))
    _, cid = rt.invoke("update_lead", UPDATE)
    env.bridge.handle_cancel(sibling, json.dumps({"thread_id": "thread-root", "tool_call_id": cid}).encode())
    p = settle(env, rt, "thread-root", cid)
    assert p["status"] == "completed" and env.crm.effect_count == 1


def test_status_reads_are_scoped_to_the_principal_that_made_the_call(runtime_factory):
    env, srv, rt = runtime_factory()
    _, c1 = rt.invoke("update_lead", UPDATE)
    p1 = settle(env, rt, "thread-root", c1)
    _, c2 = rt.invoke("get_action_status", {"action_id": p1["action_id"]})
    assert settle(env, rt, "thread-root", c2)["output"]["status"] == "completed"
    # an unknown or foreign action id is "not found", never another caller's record
    env.store.update(env.store.newest_by_action(env.tenant, p1["action_id"])["rec_key"], principal="someone-else")
    _, c3 = rt.invoke("get_action_status", {"action_id": p1["action_id"]})
    assert settle(env, rt, "thread-root", c3)["output"] == {"found": False}


def test_the_result_never_contains_credentials_or_internal_paths(runtime_factory):
    env, srv, rt = runtime_factory()
    _, cid = rt.invoke("update_lead", UPDATE)
    settle(env, rt, "thread-root", cid)
    text = rt.results[("thread-root", cid)][0]["text"]
    assert "secret" not in text.lower() and str(env.workdir) not in text and "credential" not in text.lower()
