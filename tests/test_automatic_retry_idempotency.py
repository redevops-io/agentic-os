"""Automatic retry of a side-effecting step needs a reliable uniqueness guarantee, declared by the capability.

Owner rule: an automatic retry is prohibited when provider-enforced idempotency (or another reliable uniqueness guarantee) cannot be
established. A capability declares ``idempotency`` = ``provider-enforced`` | ``marker-reconciled`` | ``none`` (undeclared = unknown = ``none``).
The reconciliation supervisor may re-dispatch a node from an authoritative-negative probe ONLY for ``provider-enforced``. For every other
capability the node stays UNCERTAIN (``AutomaticRetryRefused`` on the log, the job escalated), nothing is released or re-sent, and only an
authenticated operator action (``reconcile_node`` with a person's principal) can re-dispatch it. Positive evidence ("it executed") never
re-sends anything and is unaffected."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_mission_park_resume import events, registry                                                   # noqa: E402
from test_reconciliation_supervisor import EV, Q_STRICT, absent, build, executed, second_calls, tick    # noqa: E402

from agentic_os.mission.operator_sdk import capability                                                  # noqa: E402
from agentic_os.mission.production import _spec_from_json                                               # noqa: E402
from agentic_os.mission.principal import PrincipalRequired                                             # noqa: E402
from agentic_os.mission.reconcile import ReconciliationError                                            # noqa: E402
from agentic_os.mission.types import CapabilitySpec, MissionState                                       # noqa: E402
from tests._principals import authn                                                                     # noqa: E402

NEG = [("op.*", absent(), Q_STRICT)]


def node_of(x):
    return x.rt.repo.pending_reconciliation(x.mid)[0]["node_id"]


# ── the supervisor does not re-dispatch what is not provider-idempotent ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("declared", ["", "none", "marker-reconciled"])
def test_an_authoritative_negative_does_not_redispatch_a_step_that_is_not_provider_idempotent(tmp_path, monkeypatch, declared):
    x = build(tmp_path, monkeypatch, probes=NEG, idempotency=declared)
    lease = x.lease()
    rep = tick(x, 31)
    assert rep.escalated == [lease] and not rep.resolved
    assert second_calls(x) == 0 and x.w.provider.effects.get("second", 0) == 0                    # the action was NOT sent again
    assert x.rt._missions[x.mid].state == MissionState.WAITING_RECONCILIATION
    assert x.rt.authority.lease(lease)["state"] in ("dispatched", "unknown")                      # the reservation was not released
    assert not events(x.rt, x.mid, "ReconciliationResolved")
    refused = events(x.rt, x.mid, "AutomaticRetryRefused")
    assert len(refused) == 1 and refused[0]["capability"] == "op.second" and refused[0]["uncertain"] is True
    assert refused[0]["idempotency"] == (declared or "none") and refused[0]["actor"].startswith("system:reconciliation:")
    job = x.jobs.get(lease)
    assert job["state"] == "escalated" and "idempotency" in (job["escalation_reason"] or "")


def test_the_escalated_step_is_never_retried_by_later_passes(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, probes=NEG)
    tick(x, 31)
    for _ in range(3):
        tick(x, 4000)
    assert second_calls(x) == 0 and x.rt._missions[x.mid].state == MissionState.WAITING_RECONCILIATION


def test_an_authenticated_operator_can_redispatch_the_uncertain_step(tmp_path, monkeypatch, no_test_policy):
    x = build(tmp_path, monkeypatch, probes=NEG)
    tick(x, 31)
    assert second_calls(x) == 0
    with pytest.raises(PrincipalRequired):                                                         # a person is required: no anonymous action
        x.rt.reconcile_node(x.mid, node_of(x), executed=False, evidence=EV, reason="the owner decided to send it again")
    assert second_calls(x) == 0
    x.rt.reconcile_node(x.mid, node_of(x), executed=False, evidence=EV, reason="the owner decided to send it again",
                        principal=authn("ops-person", tenant="*"))
    assert second_calls(x) == 1 and x.rt._missions[x.mid].state == MissionState.SUCCEEDED
    res = events(x.rt, x.mid, "ReconciliationResolved")[0]
    assert res["executed"] is False and res["actor"] == "ops-person"


def test_the_system_principal_cannot_release_the_reservation_either(tmp_path, monkeypatch):
    """Releasing the reservation is what re-opens the retry; resume=False would only defer the send, not prevent it."""
    x = build(tmp_path, monkeypatch, probes=NEG)
    sysp = x.sup._system_principal("ledgerco")
    lease = x.lease()
    with pytest.raises(ReconciliationError, match="idempotency"):
        x.rt.reconcile_node(x.mid, node_of(x), executed=False, evidence=EV, reason="the probe says it did not happen", principal=sysp, resume=False)
    assert x.rt.authority.lease(lease)["state"] in ("dispatched", "unknown") and second_calls(x) == 0


# ── provider-enforced: the one case where the supervisor may re-dispatch ───────────────────────────────────────────────────────────
def test_a_provider_enforced_step_is_still_redispatched_once_after_an_authoritative_negative(tmp_path, monkeypatch):
    x = build(tmp_path, monkeypatch, probes=NEG, idempotency="provider-enforced")
    rep = tick(x, 31)
    assert rep.resolved == [x.lease()] and not rep.escalated
    assert second_calls(x) == 1 and x.rt._missions[x.mid].state == MissionState.SUCCEEDED
    assert not events(x.rt, x.mid, "AutomaticRetryRefused")


# ── positive evidence never re-sends, whatever the declaration ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("declared", ["", "provider-enforced"])
def test_positive_execution_evidence_is_unaffected(tmp_path, monkeypatch, declared):
    x = build(tmp_path, monkeypatch, mode="crash_after_effect", probes=[("op.*", executed(), Q_STRICT)], idempotency=declared)
    rep = tick(x, 31)
    assert rep.resolved and x.rt._missions[x.mid].state == MissionState.SUCCEEDED and second_calls(x) == 0


# ── the declaration itself ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_default_is_undeclared_which_is_unsafe_to_repeat():
    assert CapabilitySpec("x.y", "x").idempotency == ""
    assert registry().get("op.second").idempotency == ""


def test_a_declaration_is_validated_and_carried_by_the_builder():
    assert CapabilitySpec("x.y", "x", idempotency="provider-enforced").idempotency == "provider-enforced"
    with pytest.raises(ValueError, match="idempotency"):
        CapabilitySpec("x.y", "x", idempotency="exactly-once")
    cap = capability("x.y", lambda i: {}, side_effecting=True, idempotency="marker-reconciled")
    assert cap.spec.idempotency == "marker-reconciled"
    with pytest.raises(ValueError, match="idempotency"):
        capability("x.y", lambda i: {}, side_effecting=True, idempotency="yes")


def test_a_remote_manifest_cannot_declare_its_own_provider_enforced_idempotency():
    spec = _spec_from_json({"name": "x.y", "operator": "x", "side_effecting": True, "idempotency": "provider-enforced"})
    assert spec.idempotency == ""


# ── backward compatible for read-only nodes ──────────────────────────────────────────────────────────────────────────────────────
def test_a_read_only_node_is_still_redispatched_automatically_after_an_authoritative_negative(tmp_path, monkeypatch):
    """No side effect, nothing to duplicate: the declaration is about side effects only."""
    import test_mission_park_resume as park
    real = park.registry

    def read_only_second(idempotency=""):
        reg = real(idempotency)
        reg.get("op.second").side_effecting = False
        return reg
    monkeypatch.setattr(park, "registry", read_only_second)
    x = build(tmp_path, monkeypatch, probes=NEG)
    pend = x.rt.repo.pending_reconciliation(x.mid)
    assert pend and pend[0]["capability"] == "op.second"
    rep = tick(x, 31)
    assert rep.resolved == [x.lease()] and not rep.escalated
    assert second_calls(x) == 1 and x.rt._missions[x.mid].state == MissionState.SUCCEEDED
    assert not events(x.rt, x.mid, "AutomaticRetryRefused")


# ── fork branches obey the same rule (they are reconciled through the same call) ────────────────────────────────────────────────────
def _fork_unknown_branch(tmp_path, monkeypatch, declared):
    """A fork whose branch B went UNKNOWN (sent, timed out, never applied) and a supervisor whose probe says, authoritatively, that it did not happen."""
    import random
    import time
    import test_fork_reconciliation_parity as fp
    from agentic_os.mission.probes import ProbeRegistry
    from agentic_os.mission.reconcile_jobs import ReconcileJobs, ReconciliationSupervisor, RetryPolicy
    real = fp.build_registry

    def declaring(*a, **k):
        reg = real(*a, **k)
        if declared:
            reg.get("w.write").idempotency = declared
        return reg
    monkeypatch.setattr(fp, "build_registry", declaring)
    w = fp.World(tmp_path, monkeypatch)
    w.op.applied_on_timeout = False
    rt = w.boot()
    m = w.mission(rt)
    w.op.timeout_keys = {fp.key(m.id, "B")}
    spec = fp.three(max_b=2)
    assert rt.forks.run_fork(m.id, spec).status == "awaiting_reconciliation"
    rt.forks.register_spec(m.id, spec)
    reg = ProbeRegistry()
    reg.register("w.*", absent(), Q_STRICT)
    now = [time.time()]
    sup = ReconciliationSupervisor(rt, ReconcileJobs(str(tmp_path / "jobs.db")), reg, owner="sup-1", clock=lambda: now[0], rng=random.Random(1),
                                   policy=RetryPolicy(max_probes=3, initial_s=1, max_s=5, factor=2.0, jitter=0.0))
    assert sup.scan() == 1
    now[0] += 60
    return w, rt, m, sup, fp


def test_a_fork_branch_is_not_retried_automatically_unless_its_capability_is_provider_enforced(tmp_path, monkeypatch):
    w, rt, m, sup, fp = _fork_unknown_branch(tmp_path, monkeypatch, declared="")
    rep = sup.tick()
    assert rep.escalated and not rep.resolved
    assert w.op.sent(fp.key(m.id, "B", 2)) == 0 and w.op.sent(fp.key(m.id, "B", 1)) == 1                    # the branch was NOT sent again
    assert fp.status(rt, m.id)["B"] == "reconciling" and fp.lease(rt.authority, m.id, "B", 1) in ("dispatched", "unknown")
    refused = events(rt, m.id, "AutomaticRetryRefused")
    assert len(refused) == 1 and refused[0]["capability"] == "w.write" and refused[0]["uncertain"] is True


def test_a_fork_branch_of_a_provider_enforced_capability_is_retried_as_before(tmp_path, monkeypatch):
    w, rt, m, sup, fp = _fork_unknown_branch(tmp_path, monkeypatch, declared="provider-enforced")
    rep = sup.tick()
    assert rep.resolved and not rep.escalated
    assert w.op.sent(fp.key(m.id, "B", 2)) == 1 and fp.fold_fork(rt.store.for_mission(m.id), fp.FID).closed == "satisfied"
    assert not events(rt, m.id, "AutomaticRetryRefused")


def test_a_person_can_still_retry_the_fork_branch(tmp_path, monkeypatch):
    w, rt, m, sup, fp = _fork_unknown_branch(tmp_path, monkeypatch, declared="")
    sup.tick()
    rt.reconcile_node(m.id, fp.branch_node_id(m.id, fp.FID, "B"), executed=False, evidence=EV, reason="the owner decided to send it again",
                      principal=authn("ops@acme"))
    assert w.op.sent(fp.key(m.id, "B", 2)) == 1
