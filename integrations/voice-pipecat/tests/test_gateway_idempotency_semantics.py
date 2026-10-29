"""The bridge on top of the gateway's idempotency store (agentic-os 0.4.1): per-tenant keys bound to a fingerprint of scope and arguments,
conflict and in-progress answers, held writes, a bounded store that fails closed, replays served after revalidation, and results that an
operator seeded by reconciliation."""
from __future__ import annotations

from agentic_os.agent_gateway import (
    EgressAction, GatewayPrincipal, GatewayRequest, GatewayStatus, InMemoryIdempotencyStore)
from agentic_os.agent_gateway.idempotency import MAX_KEY_CHARS, fingerprint
from agentic_os.mission.store import MemoryEventStore
from agentic_os.overlays import Principal

from agentic_os_voice.bridge import VoiceActionBridge
from agentic_os_voice.capabilities import default_tools
from agentic_os_voice.contracts import ActionStatus, ConfirmationState, Reason, derive_action_id
from conftest import UPDATE_ARGS

C = ConfirmationState.CONFIRMED
GP = GatewayPrincipal(Principal("voice-agent", "service", (), "acme-demo"), client_id="voice-pipecat", workspace="acme-demo")


def key_of(args=UPDATE_ARGS):
    return derive_action_id("acme-demo", "update_lead", args)


def fp_of(args=UPDATE_ARGS):
    return fingerprint(tenant="acme-demo", workspace="acme-demo", subject="voice-agent", authority_key="acme-demo:voice-agent",
                       capability="crm.request_lead_update", arguments=args)


async def update(p, args=None):
    return await p.call("update_lead", args or UPDATE_ARGS, confirmation=C)


def test_action_ids_fit_the_gateway_key_bound_and_are_stable_across_processes():
    k = key_of()
    assert 1 <= len(k) <= MAX_KEY_CHARS and k == derive_action_id("acme-demo", "update_lead", dict(UPDATE_ARGS))
    assert k == "act_23ca094dace0e1fc77aad7b62477909f"          # fixed value: a change here would break the dedupe of every stored action


async def test_a_key_the_gateway_holds_for_different_arguments_is_a_failure_never_a_completion(env, make_session):
    p = make_session(env)
    other = {**UPDATE_ARGS, "status": "nurture"}
    pre = env.gateway.invoke(GatewayRequest(GP, "crm.request_lead_update", other, idempotency_key=key_of()))       # someone else used the key
    assert pre.status is GatewayStatus.OK and env.crm.effect_count == 1
    r = await update(p)
    assert (r.status, r.reason, r.retry_safe) == (ActionStatus.FAILED, Reason.IDEMPOTENCY_CONFLICT, False)
    assert r.receipt_ref == "" and env.crm.effect_count == 1 and env.crm.leads["lead_dana01"]["status"] == "nurture"
    assert [c["call"] for c in env.crm.calls].count("lead_update") == 1
    assert env.guard.outstanding("acme-demo:voice-agent") == []            # the conflict was answered before any authority was reserved


async def test_a_call_still_running_under_the_key_is_in_progress_and_is_not_sent_again(env, make_session):
    p = make_session(env)
    state, _ = env.gateway.idempotency.begin("acme-demo", key_of(), fp_of(), write=True)       # another caller holds the claim
    assert state == "new"
    r = await update(p)
    assert (r.status, r.reason) == (ActionStatus.APPROVED, Reason.IDEMPOTENCY_IN_PROGRESS) and "cannot say it is done" in r.say
    assert env.crm.calls == []
    env.gateway.idempotency.abandon("acme-demo", key_of(), fp_of())                           # that call ended without a result
    done = await update(p)
    assert done.status is ActionStatus.COMPLETED and env.crm.effect_count == 1


async def test_the_gateways_stored_result_is_served_when_the_bridge_journal_is_gone(env, make_session):
    p = make_session(env)
    first = await update(p)
    fresh = VoiceActionBridge(env.gateway, default_tools(), MemoryEventStore(), approvals=env.tap, clock=env.clock)   # a new, empty journal
    from agentic_os_voice.contracts import IdentityLevel, SessionBinding
    px = fresh.open_session(SessionBinding("call-9", "acme-demo", env.clock() + 600, IdentityLevel.VERIFIED, ("lead_dana01",), "", "voice-agent"))
    again = await update(px)
    assert again.status is ActionStatus.COMPLETED and again.replayed and again.receipt_ref == first.receipt_ref
    assert env.crm.effect_count == 1


class HoldWrites:
    """An egress policy that holds the output of the lead update (the write itself still runs)."""
    hold = True

    def decide(self, output, classes, principal, manifest):
        if self.hold and manifest.name == "crm.request_lead_update":
            return EgressAction.REQUIRE_APPROVAL, None, ("hold-lead-update",)
        return EgressAction.ALLOW, output, ()


async def test_a_held_write_is_not_pending_approval_is_not_completed_and_is_never_sent_again(env, make_session):
    policy = HoldWrites()
    env.gateway.egress = policy
    p = make_session(env)
    r = await update(p)
    assert (r.status, r.reason) == (ActionStatus.APPROVED, Reason.OUTPUT_HELD) and r.receipt_ref == ""
    assert env.crm.effect_count == 1                                   # it ran
    for _ in range(3):                                                 # the model asks again, the held outcome is replayed, not re-sent
        again = await update(p)
        assert (again.status, again.reason) == (ActionStatus.APPROVED, Reason.OUTPUT_HELD)
    assert [c["call"] for c in env.crm.calls].count("lead_update") == 1
    policy.hold = False                                                # the policy releases it
    rel = env.bridge.resume(r.action_id)
    assert rel.status is ActionStatus.COMPLETED and rel.replayed and rel.receipt_ref.startswith("crm_upd_") and env.crm.effect_count == 1


async def test_a_full_idempotency_store_fails_closed_before_anything_is_dispatched(env, make_session):
    env.gateway.idempotency = InMemoryIdempotencyStore(max_entries=1)
    p = make_session(env)
    assert (await update(p)).status is ActionStatus.COMPLETED          # the completed write holds the only slot, and a write's entry is kept
    r = await update(p, {**UPDATE_ARGS, "timeline": "later"})
    assert (r.status, r.reason, r.retry_safe) == (ActionStatus.FAILED, Reason.GATEWAY_ERROR, True)
    assert env.crm.effect_count == 1 and [c["call"] for c in env.crm.calls].count("lead_update") == 1
    assert env.guard.outstanding("acme-demo:voice-agent") == []        # nothing reserved
    env.gateway.idempotency = InMemoryIdempotencyStore()               # room again: the same request now runs
    assert (await update(p, {**UPDATE_ARGS, "timeline": "later"})).status is ActionStatus.COMPLETED and env.crm.effect_count == 2


async def test_a_result_seeded_by_reconciliation_is_replayed_by_a_new_bridge_not_sent_again(env, make_session):
    p = make_session(env)
    env.crm.fail_after_effect = TimeoutError("reply lost")
    lost = await update(p)
    assert lost.status is ActionStatus.OUTCOME_UNKNOWN
    real = next(iter(env.crm.effects_of("lead_update")))
    env.bridge.reconcile(lost.action_id, executed=True, actor="alex.reviewer", reason="checked the CRM change history",
                         evidence={"kind": "provider_record", "reference": "CRM change history, lead_dana01, today"}, result=real)
    fresh = VoiceActionBridge(env.gateway, default_tools(), MemoryEventStore(), approvals=env.tap, clock=env.clock)
    from agentic_os_voice.contracts import IdentityLevel, SessionBinding
    px = fresh.open_session(SessionBinding("call-9", "acme-demo", env.clock() + 600, IdentityLevel.VERIFIED, ("lead_dana01",), "", "voice-agent"))
    again = await update(px)
    assert again.status is ActionStatus.COMPLETED and again.replayed and again.receipt_ref == real["provider_object_id"]
    assert [c["call"] for c in env.crm.calls].count("lead_update") == 1


async def test_another_principal_without_authority_cannot_collect_the_receipt_from_the_gateway(env, make_session):
    """A bridge without the original journal, for a service principal that holds no authority: nothing completes and nothing more is sent."""
    from agentic_os_voice.contracts import IdentityLevel, SessionBinding
    p = make_session(env)
    assert (await update(p)).status is ActionStatus.COMPLETED
    fresh = VoiceActionBridge(env.gateway, default_tools(), MemoryEventStore(), approvals=env.tap, clock=env.clock)
    px = fresh.open_session(SessionBinding("call-9", "acme-demo", env.clock() + 600, IdentityLevel.VERIFIED, ("lead_dana01",), "", "other-agent"))
    env.authorizer.grants.add("crm.write")
    r = await update(px)
    assert r.status is not ActionStatus.COMPLETED and env.crm.effect_count == 1
