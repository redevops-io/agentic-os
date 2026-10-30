"""The bridge grants nothing: authority, permissions, the approval gate and the allowlist all stay with the gateway."""
import json

from conftest import UPDATE, drive, payload, settle
from redevops_infinity.demo.capabilities import default_grants


def test_an_operation_that_is_not_declared_is_refused_after_the_ack(runtime_factory):
    env, srv, rt = runtime_factory()
    for name in ("sandbox.execute", "crm.request_lead_update", "missions.delegate_goal"):
        assert rt.invoke(name, {})[0] == 400                      # a gateway capability name is not a RAP tool name (no dots)
    for name in ("sandbox_execute", "admin_reset", "crm_request_lead_update", "missions_delegate_goal"):
        code, cid = rt.invoke(name, {})
        assert code == 200
        p = settle(env, rt, "thread-root", cid)
        assert p["status"] == "denied" and p["reason"] == "not_allowlisted"
    assert env.crm.calls == []


def test_an_operation_outside_the_peers_allowlist_is_refused(runtime_factory):
    env, srv, rt = runtime_factory(peer_kwargs={"operations": frozenset({"lookup_lead"})})
    _, tools = rt.discover()[0], rt.discover()[1]
    assert {t["name"] for t in tools["tools"]} == {"lookup_lead"}
    _, cid = rt.invoke("update_lead", UPDATE)
    assert settle(env, rt, "thread-root", cid)["reason"] == "not_allowlisted" and env.crm.effect_count == 0


def test_invalid_arguments_are_a_result_not_an_effect(runtime_factory):
    env, srv, rt = runtime_factory()
    _, cid = rt.invoke("update_lead", {"lead_id": "lead_dana01", "status": "qualified; drop table"})
    p = settle(env, rt, "thread-root", cid)
    assert p["status"] == "failed" and p["reason"] == "invalid_request" and env.crm.calls == []


def test_a_value_outside_the_owners_grant_is_denied_by_the_authority_envelope(runtime_factory):
    env, srv, rt = runtime_factory()
    _, cid = rt.invoke("update_lead", {"lead_id": "lead_dana01", "status": "closed_won"})        # allowed by the schema, outside the grant
    p = settle(env, rt, "thread-root", cid)
    assert p["status"] == "denied" and p["reason"] == "policy_denied" and env.crm.calls == []


def test_a_revoked_permission_denies_new_calls_and_replays(runtime_factory):
    env, srv, rt = runtime_factory()
    _, c1 = rt.invoke("update_lead", UPDATE)
    assert settle(env, rt, "thread-root", c1)["status"] == "completed"
    env.authorizer.grants.discard("crm.write")
    _, c2 = rt.invoke("update_lead", UPDATE)                                                       # same intent: would be a replay
    p = settle(env, rt, "thread-root", c2)
    assert p["status"] == "denied" and env.crm.effect_count == 1                                   # a replay does not outlive the right to the call


def test_a_revoked_envelope_stops_the_agent(runtime_factory):
    env, srv, rt = runtime_factory()
    env.envelopes.revoke(f"{env.tenant}:infinity-runtime")
    _, cid = rt.invoke("update_lead", UPDATE)
    assert settle(env, rt, "thread-root", cid)["status"] == "denied" and env.crm.calls == []


def test_a_consequential_action_waits_for_a_named_person_and_runs_once(runtime_factory, clock):
    env, srv, rt = runtime_factory()
    args = {"contact": "lead_dana01", "body": "Hi Dana, here is the link."}
    _, cid = rt.invoke("send_followup", args)
    drive(env)
    assert rt.await_result("thread-root", cid, 0.3) is None                                       # no result yet, nothing sent
    row = env.store.get(json.dumps([env.tenant, "thread-root", cid], separators=(",", ":")))
    assert row["state"] == "awaiting_approval" and env.crm.effect_count == 0
    pending = env.inbox.pending()
    assert len(pending) == 1
    for _ in range(3):                                                                              # polling re-runs park, never approve
        clock.advance(60)
        drive(env)
    assert env.crm.effect_count == 0 and len(env.inbox.pending()) == 1
    env.inbox.approve(pending[0]["id"], by="person:ops-lead")
    env.bridge.resume_awaiting()
    p = settle(env, rt, "thread-root", cid)
    assert p["status"] == "completed" and env.crm.effect_count == 1
    clock.advance(60)
    drive(env)
    assert env.crm.effect_count == 1 and len(rt.results[("thread-root", cid)]) == 1


def test_no_rap_message_can_approve(runtime_factory):
    env, srv, rt = runtime_factory()
    args = {"contact": "lead_dana01", "body": "Hi Dana."}
    for extra in ({"approval_id": "apr_x"}, {"approved": True}, {"_approval": "x"}):
        code, _ = rt.invoke("send_followup", args, raw={
            "operation": "send_followup", "arguments": args, "id": "c_" + next(iter(extra)), "callback_url": f"{rt.cb_origin}/cb/t", "group_id": "g", **extra})
        assert code == 400
    code, cid = rt.invoke("send_followup", {**args, "approved": True})
    assert code == 200
    p = settle(env, rt, "thread-root", cid)
    assert p["status"] == "failed" and p["reason"] == "invalid_request" and env.crm.effect_count == 0
    # the user_choice message of RAP is a UI confirmation of the runtime's user; this bridge never emits one, so it is not an approval path
    assert all("user_choice" not in json.dumps(m) for ms in rt.results.values() for m in ms)


def test_the_requesting_principal_cannot_approve_its_own_request(runtime_factory):
    import pytest
    from agentic_os.agent_gateway import ApprovalError
    env, srv, rt = runtime_factory()
    _, cid = rt.invoke("send_followup", {"contact": "lead_dana01", "body": "Hi."})
    drive(env)
    with pytest.raises(ApprovalError):
        env.inbox.approve(env.inbox.pending()[0]["id"], by="infinity-runtime")
    assert env.crm.effect_count == 0


def test_a_call_limit_parks_the_next_call_for_the_owner_instead_of_running_it(runtime_factory):
    grants = (default_grants()[0], default_grants()[1].__class__("crm.request_lead_update", resources=(("lead_id", ("lead_*",)), ("status", ("qualified", "nurture"))), max_tier=1, max_calls=2))
    env, srv, rt = runtime_factory(grants=grants)
    done, cids = [], []
    for i in range(4):
        _, cid = rt.invoke("update_lead", {**UPDATE, "request_ref": f"r{i}"})
        cids.append(cid)
        drive(env)
    done = [rt.await_result("thread-root", c, 0.2) is not None for c in cids]
    assert done == [True, True, False, False] and env.crm.effect_count == 2
    states = [env.store.get(json.dumps([env.tenant, "thread-root", c], separators=(",", ":")))["state"] for c in cids]
    assert states == ["decided", "decided", "awaiting_approval", "awaiting_approval"]
