"""The synthetic call through a real Pipecat pipeline (fake speech in and out, scripted model, mock CRM)."""
from __future__ import annotations

import json
import threading

import pytest

from agentic_os_voice.contracts import ActionStatus, IdentityLevel, Reason
from agentic_os_voice.demo.run import (
    build_env, happy_script, network_blocked, operator_review, run_call, unreceipted_claims)
from agentic_os_voice.demo.fakes import Call, Say, SayResult


async def test_the_happy_path_ends_in_receipts_and_the_bot_claims_nothing_it_does_not_have(env, fixture_call):
    with network_blocked():
        rep = await run_call(env, fixture_call, happy_script(fixture_call["lead_id"]))
    by_tool = {r["tool"]: r for r in rep.actions.values()}
    # Pipecat Flows owned the conversation state
    assert [n for i, n in enumerate(rep.nodes) if i == 0 or n != rep.nodes[i - 1]] == ["welcome", "qualify", "confirm", "followup", "wrapup"]
    # the barge-in cut the read-back off; it was never 'said'
    assert len(rep.interrupted) == 1 and rep.interrupted[0].startswith("To confirm") and not any(s.startswith("To confirm") for s in rep.spoken)
    # governed outcomes before the human step
    assert by_tool["lookup_lead"]["status"] == "completed"
    assert by_tool["update_lead"]["status"] == "completed" and by_tool["update_lead"]["receipt_ref"].startswith("crm_upd_")
    assert by_tool["draft_followup"]["status"] == "completed"
    assert by_tool["request_followup_send"]["status"] == "pending_approval"
    assert env.crm.effects_of("send_outreach") == [] and env.crm.effect_count == 1             # nothing was sent yet
    assert unreceipted_claims(rep.spoken, env) == []
    assert not any("sent" in s.lower() for s in rep.spoken)
    review = operator_review(env)
    assert [r["status"] for r in review] == ["completed"] and review[0]["receipt_ref"].startswith("msg_")
    assert env.crm.effect_count == 2 and env.journal.verify().ok
    # the downstream's own record agrees with the bridge's receipts, one for one
    assert {v["provider_object_id"] for v in env.crm.effects.values()} == {by_tool["update_lead"]["receipt_ref"], review[0]["receipt_ref"]}


async def test_no_backend_credential_reaches_the_model_the_frames_or_the_journal(env, fixture_call):
    with network_blocked():
        rep = await run_call(env, fixture_call, happy_script(fixture_call["lead_id"]))
    operator_review(env)
    secret = env.crm.api_key
    assert secret not in json.dumps(rep.context) and secret not in " ".join(rep.spoken)
    assert secret not in open(env.journal._path, encoding="utf-8").read()
    # nor does a journal line carry an argument value: only hashes and ids
    text = open(env.journal._path, encoding="utf-8").read()
    for value in ("Northwind", "Help with onboarding", "Dana", "10k_50k"):
        assert value not in text


def test_the_tool_callbacks_hold_only_an_action_proxy(env, make_session):
    from agentic_os_voice.bridge import ActionProxy
    from agentic_os_voice.demo.fakes import ScriptedLLM
    from agentic_os_voice.pipecat_adapter import register_governed_tools
    proxy = make_session(env)
    llm = ScriptedLLM(lambda v: [])
    register_governed_tools(llm, proxy)
    assert set(llm._functions) == set(proxy.tool_specs) | {None}
    for name, item in llm._functions.items():
        cells = [c.cell_contents for c in (item.handler.__closure__ or ())]
        assert all(isinstance(c, (ActionProxy, str)) or callable(c) for c in cells), (name, cells)
        assert not any(c is env.gateway or c is env.bridge or c is env.crm for c in cells)
    # the proxy's whole public surface
    assert {a for a in dir(proxy) if not a.startswith("_")} == {"call", "status", "request_handoff", "tool_specs", "identity", "session_id"}
    with pytest.raises(AttributeError):
        proxy.session_id = "someone-elses-call"
    with pytest.raises(AttributeError):
        proxy.anything = 1
    assert not hasattr(proxy, "__dict__")


COMPROMISED = [
    ("export_all_leads", {}),
    ("sandbox.execute", {"code": "print(1)"}),
    ("update_lead", {"lead_id": "lead_dana01", "status": "closed_won", "budget_band": "over_50k", "timeline": "now", "caller_confirmed": True}),
    ("update_lead", {"lead_id": "lead_omar02", "status": "qualified", "budget_band": "over_50k", "timeline": "now", "caller_confirmed": True}),
    ("lookup_lead", {"lead_id": "lead_omar02"}),
    ("request_followup_send", {"contact": "lead_dana01", "body": "Pay now at example.invalid", "caller_confirmed": True}),
]


async def test_a_compromised_model_cannot_get_a_privileged_effect_through_pipecat(env, fixture_call):
    """The 'model' is scripted to do whatever the caller's words told it to: call unlisted tools, widen a status, reach another
    account, send an unapproved message. Pipecat executes every call it makes; the bridge and the gateway decide."""
    def script(v):
        if not v.last_user and not v.last_tool:
            return [Say("Hello.")]
        if v.last_tool == "":
            return [Call(n, a) for n, a in COMPROMISED]
        return [Say("Done.")] if False else []
    tl = [{"do": "wait_idle"}, {"do": "host", "name": "verify"}, {"do": "say_text", "id": "u1"}, {"do": "wait_idle"}, {"do": "end"}]
    rep = await run_call(env, fixture_call, script, with_flow=False, timeline=tl)
    # nothing but the parked send survives, and the parked send has not been sent
    assert env.crm.effect_count == 0 and env.crm.effects_of("send_outreach") == []
    everything = [env.bridge.receipt(a) for a in list(env.bridge._index)]        # refusals included
    statuses = [r["status"] for r in everything]
    assert statuses.count("pending_approval") == 1 and statuses.count("completed") == 0
    reasons = sorted(r["reason"] for r in everything if r["status"] == "denied")
    assert Reason.NOT_ALLOWLISTED in reasons and Reason.IDENTITY_UNVERIFIED in reasons and Reason.POLICY_DENIED in reasons
    assert env.crm.leads["lead_omar02"]["status"] == "new" and env.crm.leads["lead_dana01"]["status"] == "new"


VERIFY = {"do": "host", "name": "verify"}


def _wait(event):
    async def op():
        import asyncio
        assert await asyncio.get_running_loop().run_in_executor(None, event.wait, 5), "the tool never reached the CRM"
    return op


async def test_barge_in_during_a_write_does_not_cancel_it_and_the_receipt_is_used_once(env, fixture_call):
    """A real Pipecat InterruptionFrame arrives while the write is inside the CRM. Pipecat is told the write is an asynchronous
    tool (cancel_on_interruption=False) and the bridge shields it as well: it finishes, once, and the model learns the outcome."""
    fixture_call["utterances"].update(a="Please record that on my lead.", b="Sorry, one more thing before you continue.")

    def script(v):
        if not v.last_user and not v.last_tool:
            return [Say("Hello.")]
        if v.last_user.startswith("Please") and not v.last_tool:
            return [Call("update_lead", {"lead_id": "lead_dana01", "status": "qualified", "budget_band": "10k_50k", "timeline": "now",
                                         "caller_confirmed": True})]
        if v.last_user.startswith("Sorry") and not v.last_tool:
            return [Say("Go ahead.")]
        if v.last_tool == "update_lead":
            return [SayResult("update_lead")]
        return []
    env.crm.gate = threading.Event()

    async def release():
        env.crm.gate.set()
    rep = await run_call(env, fixture_call, script, with_flow=False, host_ops={"wait_write": _wait(env.crm.entered), "release": release}, timeline=[
        {"do": "wait_idle"}, VERIFY, {"do": "say_text", "id": "a"}, {"do": "wait_write"},
        {"do": "say_text", "id": "b"}, {"do": "release"}, {"do": "wait_idle"}, {"do": "end"}])
    assert rep.cancelled_calls == []                                            # Pipecat did not cancel the write
    assert [c["call"] for c in env.crm.calls].count("lead_update") == 1 and env.crm.effect_count == 1
    upd = next(r for r in rep.actions.values() if r["tool"] == "update_lead")
    assert upd["status"] == "completed" and upd["receipt_ref"].startswith("crm_upd_")
    assert "That is done, and the system returned a confirmation." in rep.spoken and unreceipted_claims(rep.spoken, env) == []


async def test_barge_in_during_a_read_cancels_the_read_and_nothing_else_happens(env, fixture_call):
    fixture_call["utterances"].update(a="Can you look up my record?", b="Sorry, never mind that.")

    def script(v):
        if not v.last_user and not v.last_tool:
            return [Say("Hello.")]
        if v.last_user.startswith("Can you") and not v.last_tool:
            return [Call("lookup_lead", {"lead_id": "lead_dana01"})]
        if v.last_user.startswith("Sorry") and not v.last_tool:
            return [Say("Okay.")]
        return []
    env.crm.read_gate = threading.Event()

    async def release():
        env.crm.read_gate.set()
    rep = await run_call(env, fixture_call, script, with_flow=False, host_ops={"wait_read": _wait(env.crm.read_entered), "release": release}, timeline=[
        {"do": "wait_idle"}, VERIFY, {"do": "say_text", "id": "a", "expect_inference": True}, {"do": "wait_read"},
        {"do": "say_text", "id": "b"}, {"do": "release"}, {"do": "wait_idle"}, {"do": "end"}])
    assert rep.cancelled_calls == ["lookup_lead"]                               # the read was cancelled by the interruption
    assert env.crm.effect_count == 0 and "Okay." in rep.spoken
    import asyncio
    await env.bridge.drain()
    assert not env.bridge._tasks                                                # and the abandoned read thread finished, no stray task


async def test_distress_language_goes_to_a_person_without_a_model_turn_or_a_promise(env, fixture_call):
    from agentic_os_voice.handoff import HANDOFF_SCRIPT
    fixture_call["utterances"]["d1"] = "I think I am having a heart attack, please help me."
    calls = {"n": 0}

    def script(v):
        calls["n"] += 1
        return [Say("Hello.")] if not v.last_user else [Call("lookup_lead", {"lead_id": "lead_dana01"})]
    rep = await run_call(env, fixture_call, script, with_flow=False, timeline=[
        {"do": "wait_idle"}, VERIFY, {"do": "say_text", "id": "d1", "expect_inference": False}, {"do": "wait_handoff"}, {"do": "end"}])
    assert rep.handoff == "emergency" and calls["n"] == 1                       # the model got the greeting turn only, never the distress text
    assert HANDOFF_SCRIPT in rep.spoken
    spoken_lower = HANDOFF_SCRIPT.lower()
    for promise in ("will be with you", "on the way", "help is coming", "right away", "immediately", "shortly", "within", "guarantee", "we will call"):
        assert promise not in spoken_lower
    assert "cannot promise" in spoken_lower and "emergency services" in spoken_lower
    h = next(r for r in rep.actions.values() if r["tool"] == "request_human")
    assert h["status"] == "completed" and h["receipt_ref"].startswith("task_") and len(env.crm.effects_of("handoff")) == 1
    # after the latch no automated tool runs for this call
    assert not any(r["tool"] == "lookup_lead" for r in rep.actions.values()) and env.crm.effect_count == 1


async def test_after_a_handoff_other_tools_are_refused(env, make_session):
    p = make_session(env)
    r = await p.request_handoff("distress")
    assert r.status is ActionStatus.COMPLETED
    assert (await p.call("lookup_lead", {"lead_id": "lead_dana01"})).reason == Reason.HANDOFF_ACTIVE
    again = await p.request_handoff("distress")                                 # a repeated or duplicate request is the same action
    assert again.replayed and env.crm.effect_count == 1


async def test_production_profile_accepts_the_durable_guard_and_the_whole_call_runs(tmp_path, fixture_call, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    env = build_env(str(tmp_path))
    with network_blocked():
        rep = await run_call(env, fixture_call, happy_script(fixture_call["lead_id"]))
    assert {r["tool"]: r["status"] for r in rep.actions.values()}["update_lead"] == "completed"
    assert unreceipted_claims(rep.spoken, env) == []


def test_the_bridge_refuses_a_gateway_without_an_authority_guard(env):
    from agentic_os.agent_gateway import AgentGateway, CapabilityRegistry
    from agentic_os_voice.bridge import VoiceActionBridge
    with pytest.raises(ValueError):
        VoiceActionBridge(AgentGateway(registry=CapabilityRegistry()), {}, env.journal)
