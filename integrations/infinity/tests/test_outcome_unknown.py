"""Definitive downstream receipts; anything ambiguous is outcome_unknown and is never sent again until a person reconciles it."""
import pytest

EV = {"kind": "provider_log", "reference": "crm-audit-2026-10-29"}

from conftest import UPDATE, drive, payload, settle


def test_completed_carries_the_downstream_receipt(runtime_factory):
    env, srv, rt = runtime_factory()
    _, cid = rt.invoke("update_lead", UPDATE)
    p = settle(env, rt, "thread-root", cid)
    assert p["status"] == "completed" and p["receipt_ref"].startswith("crm_update_")


def test_success_without_a_receipt_is_not_completed(runtime_factory):
    env, srv, rt = runtime_factory()
    env.crm.no_receipt = True
    _, cid = rt.invoke("update_lead", UPDATE)
    p = settle(env, rt, "thread-root", cid)
    assert p["status"] == "outcome_unknown" and p["reason"] == "no_downstream_receipt" and "receipt_ref" not in p
    assert rt.results[("thread-root", cid)][0]["text"].startswith("Error: ")


def test_a_lost_answer_is_outcome_unknown_and_blocks_a_resend(runtime_factory):
    env, srv, rt = runtime_factory()
    env.crm.lose_answer_once = True
    _, c1 = rt.invoke("update_lead", UPDATE)
    p1 = settle(env, rt, "thread-root", c1)
    assert p1["status"] == "outcome_unknown" and p1["retry_safe"] is False and env.crm.effect_count == 1
    for i in range(3):                                                  # the model asks again, again and again
        _, c = rt.invoke("update_lead", UPDATE)
        assert settle(env, rt, "thread-root", c)["status"] == "outcome_unknown"
    assert env.crm.effect_count == 1 and env.crm.calls.count("update") == 1


def test_an_error_from_a_write_is_unknown_even_if_the_effect_never_happened(runtime_factory):
    """The gateway cannot tell an error before the effect from an error after it, so a failed write is kept UNKNOWN (counted against the
    authority) and is never sent again until a person reconciles it. Uncertainty is not permission to resend."""
    env, srv, rt = runtime_factory()
    env.crm.fail_before_effect_once = True
    _, c1 = rt.invoke("update_lead", UPDATE)
    assert settle(env, rt, "thread-root", c1)["status"] == "outcome_unknown"
    _, c2 = rt.invoke("update_lead", UPDATE)
    assert settle(env, rt, "thread-root", c2)["status"] == "outcome_unknown"
    assert env.crm.effect_count == 0 and env.crm.calls.count("update") == 1


def test_reconcile_executed_turns_unknown_into_a_replayable_completion(runtime_factory):
    env, srv, rt = runtime_factory()
    env.crm.lose_answer_once = True
    _, c1 = rt.invoke("update_lead", UPDATE)
    p1 = settle(env, rt, "thread-root", c1)
    with pytest.raises(ValueError):                                      # an executed outcome needs the receipt
        env.bridge.reconcile(env.tenant, p1["action_id"], executed=True, evidence=EV, actor="person:ops", reason="checked the CRM log", result={})
    out = env.bridge.reconcile(env.tenant, p1["action_id"], executed=True, evidence=EV, actor="person:ops",
                               reason="checked the CRM audit log by hand", result={"provider_object_id": "crm_update_manual"})
    assert out["status"] == "completed" and out["receipt_ref"] == "crm_update_manual"
    _, c2 = rt.invoke("update_lead", UPDATE)
    p2 = settle(env, rt, "thread-root", c2)
    assert p2["status"] == "completed" and p2["reason"] == "replayed" and p2["receipt_ref"] == "crm_update_manual" and env.crm.effect_count == 1


def test_reconcile_not_executed_allows_one_new_attempt(runtime_factory):
    env, srv, rt = runtime_factory()
    env.crm.lose_answer_once = True
    _, c1 = rt.invoke("update_lead", UPDATE)
    p1 = settle(env, rt, "thread-root", c1)
    env.bridge.reconcile(env.tenant, p1["action_id"], executed=False, evidence=EV, actor="person:ops", reason="the CRM shows no change")
    # the provider already holds the key (the synthetic CRM did execute), so the retry is answered by the provider's own idempotency, not a second effect
    _, c2 = rt.invoke("update_lead", UPDATE)
    assert settle(env, rt, "thread-root", c2)["status"] == "completed" and env.crm.effect_count == 1


def test_reconcile_refuses_what_is_not_unknown_and_needs_a_named_person(runtime_factory):
    env, srv, rt = runtime_factory()
    _, c1 = rt.invoke("update_lead", UPDATE)
    p1 = settle(env, rt, "thread-root", c1)
    with pytest.raises(KeyError):
        env.bridge.reconcile(env.tenant, p1["action_id"], executed=False, evidence=EV, actor="person:ops", reason="not unknown at all")
    env.crm.lose_answer_once = True
    _, c2 = rt.invoke("update_lead", {**UPDATE, "status": "nurture"})
    p2 = settle(env, rt, "thread-root", c2)
    from agentic_os.mission.reconcile import ReconciliationError
    with pytest.raises(ReconciliationError):
        env.bridge.reconcile(env.tenant, p2["action_id"], executed=False, evidence=EV, actor="", reason="no one named here")
