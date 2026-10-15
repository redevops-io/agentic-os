"""v6 Phase 6.1 — deployment is a governed mission.

CD runs *through* the mission runtime: an `infra` operator wraps Terraform (plan/apply/destroy)
and Ansible (configure/rollback), and edge-sentinel supplies the supply-chain scan. The `deploy_app`
template then gets, for free, everything a mission has — the provision step gates on human approval,
and a failed verify unwinds the committed provision + configure newest-first via their saga undos
(terraform-destroy the delta, ansible-redeploy the prior release). The infra handlers here stand in
for the real `terraform`/`ansible` shell-outs; the mission mechanics are the point.
"""
from __future__ import annotations

from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import Operator, LocalOperatorClient, capability
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState

GRANTS = ["security:read", "infra:write"]


def build_deploy_fleet(verify_fails: bool = False):
    """edge-sentinel (supply-chain scan) + an infra operator (terraform/ansible), with saga undos.
    Undo handlers record into `undone` so a rollback is observable."""
    undone: list[str] = []

    def _verify(_i):
        if verify_fails:
            raise RuntimeError("health check failed after rollout")
        return {"healthy": True, "capabilities": 3}

    sentinel = Operator("edge-sentinel", [
        capability("edge-sentinel.supply_chain_scan", lambda i: {"cves_critical": 0, "sbom": "ok"},
                   provides=["image_scanned"], permissions=["security:read"],
                   estimated_value="high", latency_ms=4000),
    ])
    infra = Operator("infra", [
        capability("infra.plan", lambda i: {"adds": 3, "changes": 1, "destroys": 0},
                   provides=["infra_planned"], permissions=["infra:write"], latency_ms=3000),
        capability("infra.provision", lambda i: {"applied": True},
                   provides=["infra_provisioned"], side_effecting=True, approval_required=True,
                   undo="infra.destroy_delta", permissions=["infra:write"],
                   estimated_value="high", latency_ms=20000),
        capability("infra.configure", lambda i: {"released": "v-new"},
                   provides=["app_configured"], side_effecting=True, undo="infra.rollback_release",
                   permissions=["infra:write"], latency_ms=15000),
        capability("infra.verify", _verify,
                   provides=["deploy_verified"], permissions=["infra:write"], latency_ms=5000),
        capability("infra.destroy_delta",
                   lambda i: (undone.append("infra.destroy_delta"), {"destroyed": True})[1],
                   side_effecting=True, permissions=["infra:write"]),
        capability("infra.rollback_release",
                   lambda i: (undone.append("infra.rollback_release"), {"rolled_back": True})[1],
                   side_effecting=True, permissions=["infra:write"]),
    ])
    ops = {op.name: op for op in (sentinel, infra)}
    reg = CapabilityRegistry()
    for op in ops.values():
        reg.register(op.manifest)
    return reg, LocalOperatorClient(ops), undone


def _runtime(verify_fails=False):
    reg, client, undone = build_deploy_fleet(verify_fails=verify_fails)
    return MissionRuntime(reg, Executor(client), store=EventStore()), undone


def test_deploy_is_gated_on_provision_then_succeeds():
    rt, _ = _runtime()
    m = rt.create_mission("Deploy agentic-billing", policy_refs=GRANTS, template="deploy_app")
    rt.run(m.id)

    # terraform apply is the highest-consequence step — it parks for human approval
    assert m.state == MissionState.WAITING_HUMAN
    pending = rt.repo.pending_human(m.id)
    assert pending and pending["capability"] == "infra.provision"

    rt.approve(m.id, pending["node_id"], "approve")
    assert rt._missions[m.id].state == MissionState.SUCCEEDED
    world = rt._world(m.id).snapshot()
    assert {"image_scanned", "infra_planned", "infra_provisioned",
            "app_configured", "deploy_verified"} <= set(world)


def _failed_deploy(verify_fails=True, **create):
    rt, undone = _runtime(verify_fails=verify_fails)
    m = rt.create_mission("Deploy agentic-billing", policy_refs=GRANTS, template="deploy_app", **create)
    rt.run(m.id)
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")   # provision + configure commit, verify fails
    return rt, m, undone


def test_failed_verify_preserves_the_working_deployment_and_reports_partial_completion():
    """deploy_app is PRESERVE_ONLY: a failed verify must NOT auto-roll back a successfully applied/configured stack."""
    from agentic_os.mission.compensation_runner import attention_items
    rt, m, undone = _failed_deploy()
    assert rt._missions[m.id].state == MissionState.FAILED
    assert undone == []                                                   # nothing was reversed
    types = [e["type"] for e in rt.repo.timeline(m.id)]
    assert not any(t.startswith("Compensation") or t == "NodeCompensated" for t in types)
    out = rt._missions[m.id].outcome
    assert out["failure_policy"] == "PRESERVE_ONLY" and out["rolled_back"] is False
    assert out["partially_completed"] is True
    assert [r["capability"] for r in out["retained_effects"]] == ["infra.configure", "infra.provision"]
    assert {r["reason"] for r in out["retained_effects"]} == {"left_by_policy"}
    assert [c["capability"] for c in out["completed_steps"]] == [
        "edge-sentinel.supply_chain_scan", "infra.plan", "infra.provision", "infra.configure"]
    assert [i["capability"] for i in out["incomplete_steps"]] == ["infra.verify"]
    assert "health check failed" in out["incomplete_steps"][0]["error"]
    assert {a["action"] for a in out["recovery_actions"]} >= {"start_new_mission_for_remaining_steps",
                                                              "decide_explicit_compensation"}
    assert "EffectsRetained" in types
    item = [i for i in attention_items(rt, m.id) if i["kind"] == "effects_left_in_place"][0]
    assert item["retained_effects"] and item["incomplete_steps"] and item["completed_steps"] and item["recovery_actions"]


def test_failed_verify_rolls_the_deploy_back_only_when_compensation_is_explicitly_opted_in():
    rt, m, undone = _failed_deploy(failure_policy="COMPENSATE_DECLARED")
    assert rt._missions[m.id].state == MissionState.FAILED
    # the committed provision + configure are unwound newest-first via their undos - on explicit opt-in only
    assert set(undone) == {"infra.destroy_delta", "infra.rollback_release"}
    comp = [e for e in rt.repo.timeline(m.id) if e["type"] == "CompensationExecuted"]
    assert len(comp) == 2


def test_deployment_env_can_opt_in_to_compensation(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_DEFAULT_FAILURE_POLICY", "COMPENSATE_DECLARED")
    rt, m, undone = _failed_deploy()
    assert set(undone) == {"infra.destroy_delta", "infra.rollback_release"}


class CountingClient:
    """Counts every dispatch OUTSIDE the event log and BELOW the operator's own idempotency cache, so a repeated
    side-effecting call is seen even when the operator would have deduplicated it."""

    def __init__(self, inner):
        self.inner = inner
        self.counts: dict[str, int] = {}

    def invoke(self, operator, capability, inputs, idempotency_key, **kw):
        self.counts[capability] = self.counts.get(capability, 0) + 1
        return self.inner.invoke(operator, capability, inputs, idempotency_key, **kw)


def test_resume_after_restart_does_not_repeat_completed_consequential_steps():
    reg, client, _ = build_deploy_fleet()
    cc = CountingClient(client)
    store = EventStore()
    rt = MissionRuntime(reg, Executor(cc), store=store)
    m = rt.create_mission("Deploy", policy_refs=GRANTS, template="deploy_app")
    rt.run(m.id)                                                  # parks on the provision gate
    rt2 = MissionRuntime(reg, Executor(cc), store=store)         # restart: brand-new runtime, same log
    rt2.rehydrate(m.id)
    rt2.approve(m.id, rt2.repo.pending_human(m.id)["node_id"], "approve")
    assert rt2._missions[m.id].state == MissionState.SUCCEEDED
    rt2.run(m.id)                                                 # a redundant resume is a no-op
    for cap in ("edge-sentinel.supply_chain_scan", "infra.plan", "infra.provision", "infra.configure",
                "infra.verify"):
        assert cc.counts[cap] == 1, (cap, cc.counts)


def test_failed_mission_is_terminal_and_a_second_run_repeats_nothing():
    """After a PRESERVE_ONLY failure, run() does not re-dispatch the preserved provision/configure."""
    reg, client, undone = build_deploy_fleet(verify_fails=True)
    cc = CountingClient(client)
    rt = MissionRuntime(reg, Executor(cc), store=EventStore())
    m = rt.create_mission("Deploy", policy_refs=GRANTS, template="deploy_app")
    rt.run(m.id)
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    before = dict(cc.counts)
    rt.run(m.id)
    rt.run(m.id)
    assert cc.counts == before and before["infra.provision"] == 1 and before["infra.configure"] == 1
    assert undone == []


def test_deploy_needs_infra_grant():
    from agentic_os.mission.compiler import compile_intent, CompileError
    from agentic_os.mission.templates import deploy_app
    from agentic_os.mission.types import Mission
    import pytest

    reg, _, _ = build_deploy_fleet()
    m = Mission(goal="deploy", policy_refs=["security:read"])  # missing infra:write
    with pytest.raises(CompileError) as ei:
        compile_intent(m, deploy_app(m.id), reg)
    assert "permission denied" in str(ei.value)
