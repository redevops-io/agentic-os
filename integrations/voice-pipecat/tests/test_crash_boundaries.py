"""Crash at the write boundaries, using the repository's CrashingStore, and a downstream that loses its answer.

The journal append is the bridge's write boundary. The CRM write is the downstream's. The sequence of one write is
  journal 'submitted' -> gateway (authority lease, handler -> CRM effect, lease settled) -> journal 'decided'.
"""
from __future__ import annotations

import pytest

from agentic_os_voice.contracts import ActionStatus, ConfirmationState, Reason
from agentic_os_voice.demo.mock_crm import MockCrm
from agentic_os_voice.demo.run import build_env
from conftest import UPDATE_ARGS

C = ConfirmationState.CONFIRMED


def crash_env(tmp_path, clock, fork_support, mode, crm=None):
    from agentic_os_voice.demo.run import build_env
    store = fork_support.CrashingStore(str(tmp_path / "voice-journal.jsonl"), mode)
    return build_env(str(tmp_path), journal=store, clock=clock, crm=crm), store


def restart(tmp_path, clock, crm):
    return build_env(str(tmp_path), crm=crm, clock=clock)


def open_and_arm(env, store, n, make_session):
    p = make_session(env)
    store.arm(n)                       # counts appends AFTER the session was opened: 1 = 'submitted', 2 = 'decided'
    return p


@pytest.mark.parametrize("mode", ["before", "after"])
async def test_crash_at_the_submitted_event_means_no_dispatch_and_a_safe_retry(tmp_path, clock, fork_support, make_session, mode):
    from agentic_os.mission.fork_join import SimulatedCrash
    crm = MockCrm(str(tmp_path / "crm.json"))
    env, store = crash_env(tmp_path, clock, fork_support, mode, crm)
    p = open_and_arm(env, store, 1, make_session)
    with pytest.raises(SimulatedCrash):
        await p.call("update_lead", UPDATE_ARGS, confirmation=C)
    assert crm.effect_count == 0 and crm.calls == []                  # nothing was dispatched
    env2 = restart(tmp_path, clock, crm)
    p2 = make_session(env2)
    r = await p2.call("update_lead", UPDATE_ARGS, confirmation=C)
    assert r.status is ActionStatus.COMPLETED and crm.effect_count == 1
    assert env2.journal.verify().ok


async def test_crash_before_the_outcome_is_journaled_recovers_the_receipt_from_the_authority_ledger(tmp_path, clock, fork_support, make_session):
    from agentic_os.mission.fork_join import SimulatedCrash
    crm = MockCrm(str(tmp_path / "crm.json"))
    env, store = crash_env(tmp_path, clock, fork_support, "before", crm)
    p = open_and_arm(env, store, 2, make_session)
    with pytest.raises(SimulatedCrash):
        await p.call("update_lead", UPDATE_ARGS, confirmation=C)
    assert crm.effect_count == 1                                       # the effect happened, the process died before saying so
    env2 = restart(tmp_path, clock, crm)
    p2 = make_session(env2)
    aid = next(iter(env2.bridge._index))
    rec = env2.bridge.receipt(aid)
    assert rec["status"] == "completed" and rec["receipt_ref"].startswith("crm_upd_")        # recovered from the ledger, not guessed
    r = await p2.call("update_lead", UPDATE_ARGS, confirmation=C)
    assert r.replayed and r.receipt_ref == rec["receipt_ref"] and crm.effect_count == 1        # and NOT repeated


async def test_crash_after_the_outcome_is_journaled(tmp_path, clock, fork_support, make_session):
    from agentic_os.mission.fork_join import SimulatedCrash
    crm = MockCrm(str(tmp_path / "crm.json"))
    env, store = crash_env(tmp_path, clock, fork_support, "after", crm)
    p = open_and_arm(env, store, 2, make_session)
    with pytest.raises(SimulatedCrash):
        await p.call("update_lead", UPDATE_ARGS, confirmation=C)
    env2 = restart(tmp_path, clock, crm)
    r = await make_session(env2).call("update_lead", UPDATE_ARGS, confirmation=C)
    assert r.replayed and crm.effect_count == 1


async def test_a_process_death_inside_the_downstream_call_is_unknown_and_never_resent(tmp_path, clock, fork_support, make_session):
    from agentic_os.mission.fork_join import SimulatedCrash
    crm = MockCrm(str(tmp_path / "crm.json"))
    env, _ = crash_env(tmp_path, clock, fork_support, "after", crm)
    p = make_session(env)
    crm.fail_after_effect = SimulatedCrash("killed after the CRM applied the write")
    with pytest.raises(SimulatedCrash):
        await p.call("update_lead", UPDATE_ARGS, confirmation=C)
    assert crm.effect_count == 1
    env2 = restart(tmp_path, clock, crm)
    p2 = make_session(env2)
    r = await p2.call("update_lead", UPDATE_ARGS, confirmation=C)
    assert (r.status, r.reason) == (ActionStatus.OUTCOME_UNKNOWN, Reason.OUTCOME_UNKNOWN)
    assert [c["call"] for c in crm.calls].count("lead_update") == 1 and crm.effect_count == 1       # the CRM was not asked again


async def test_a_lost_response_is_unknown_not_failed_and_reconciling_with_evidence_settles_it(env, make_session):
    p = make_session(env)
    env.crm.fail_after_effect = TimeoutError("response lost")          # the CRM applied the write; the caller never heard
    first = await p.call("update_lead", UPDATE_ARGS, confirmation=C)
    assert (first.status, first.reason) == (ActionStatus.OUTCOME_UNKNOWN, Reason.OUTCOME_UNKNOWN) and not first.retry_safe
    for _ in range(3):                                                  # the model asks again: it is refused, not sent
        again = await p.call("update_lead", UPDATE_ARGS, confirmation=C)
        assert again.status is ActionStatus.OUTCOME_UNKNOWN
    assert [c["call"] for c in env.crm.calls].count("lead_update") == 1 and env.crm.effect_count == 1
    assert "that went through" in first.say and "cannot confirm" in first.say.lower()
    # a named person checks the CRM and reconciles with the downstream's own receipt
    real = next(iter(env.crm.effects_of("lead_update")))
    done = env.bridge.reconcile(first.action_id, executed=True, actor="alex.reviewer", reason="checked the CRM change history",
                                evidence={"kind": "provider_record", "reference": "CRM change history, lead_dana01, today"}, result=real)
    assert done.status is ActionStatus.COMPLETED and done.receipt_ref == real["provider_object_id"]
    r = await p.call("update_lead", UPDATE_ARGS, confirmation=C)
    assert r.replayed and r.receipt_ref == real["provider_object_id"] and env.crm.effect_count == 1


async def test_a_failure_before_any_effect_is_safe_to_retry(env, make_session):
    p = make_session(env)
    env.crm.fail_before_effect = ConnectionError("CRM unreachable")
    first = await p.call("update_lead", UPDATE_ARGS, confirmation=C)
    # the gateway reserved and dispatched before the handler raised, so it cannot prove the effect did not happen: unknown, not failed
    assert first.status is ActionStatus.OUTCOME_UNKNOWN and env.crm.effect_count == 0
    env.bridge.reconcile(first.action_id, executed=False, actor="alex.reviewer", reason="CRM change history shows no change",
                         evidence={"kind": "provider_record", "reference": "CRM change history, lead_dana01, today"})
    r = await p.call("update_lead", UPDATE_ARGS, confirmation=C)
    assert r.status is ActionStatus.COMPLETED and env.crm.effect_count == 1


async def test_journal_unavailable_means_nothing_is_dispatched(env, make_session, monkeypatch):
    p = make_session(env)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(env.journal, "append", boom)
    r = await p.call("update_lead", UPDATE_ARGS, confirmation=C)
    assert (r.status, r.reason) == (ActionStatus.FAILED, Reason.BRIDGE_UNAVAILABLE) and r.retry_safe
    assert env.crm.calls == [] and env.crm.effect_count == 0
