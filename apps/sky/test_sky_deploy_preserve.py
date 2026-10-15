"""sky_deploy is PRESERVE_ONLY (docs/COMPENSATION.md): a failed verify never tears down the provisioned cluster; the
mission reports it as still provisioned with its estimated cost (or an explicit "unknown"). Teardown (`sky.down`) runs
only on an explicit failure_policy opt-in. A counting runner proves resume/retry never re-launches."""
from __future__ import annotations

from agentic_os.mission.compensation_runner import attention_items
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient, Operator, capability
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState

from sky.operator import build_sky_operator

_DRYRUN = """\
 CLOUD   INSTANCE        vCPUs   Mem(GB)   ACCELERATORS   REGION/ZONE     COST ($)   CHOSEN
 GCP     g2-standard-4   4       16        L4:1           us-central1     0.85          ✔
 AWS     g5.xlarge       4       16        A10G:1         us-east-1       1.01
-------------------------------------------------------------------------------------------------
"""


def _fleet(*, dryrun=_DRYRUN, launch=(0, "Launching on GCP g2-standard-4\nEndpoint: http://34.1.2.3:8000\n", "")):
    runs: list[list[str]] = []

    def run(argv, cwd=None):
        runs.append(list(argv))
        j = " ".join(argv)
        if "--dryrun" in j:
            return 0, dryrun, ""
        if "sky check" in j:
            return 0, "  GCP: enabled\n", ""
        if "sky launch" in j:
            return launch
        return 0, "", ""

    verifier = Operator("verifier", [capability(
        "deploy.verify", lambda i: (_ for _ in ()).throw(RuntimeError("endpoint unhealthy")),
        provides=["deploy_verified"], permissions=["*"])])
    sky = build_sky_operator(run=run)
    ops = {"sky": sky, "verifier": verifier}
    reg = CapabilityRegistry()
    for o in ops.values():
        reg.register(o.manifest)
    return reg, LocalOperatorClient(ops), runs


class _Counting:
    def __init__(self, inner):
        self.inner, self.counts = inner, {}

    def invoke(self, operator, cap, inputs, key, **kw):
        self.counts[cap] = self.counts.get(cap, 0) + 1
        return self.inner.invoke(operator, cap, inputs, key, **kw)


def _fail_deploy(fleet_kw=None, **create):
    reg, client, runs = _fleet(**(fleet_kw or {}))
    cc = _Counting(client)
    rt = MissionRuntime(reg, Executor(cc), store=EventStore())
    m = rt.create_mission("place a GPU job", policy_refs=["*"], template="sky_deploy", **create)
    rt.run(m.id)
    while m.state == MissionState.WAITING_HUMAN:
        rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    return rt, m, runs, cc


def _downs(runs):
    return [r for r in runs if r[:2] == ["sky", "down"]]


def test_failed_verify_preserves_cluster_and_reports_resources_and_cost():
    rt, m, runs, _ = _fail_deploy()
    assert m.state == MissionState.FAILED
    assert _downs(runs) == []                                           # no sky down: the cluster is KEPT
    types = [e["type"] for e in rt.repo.timeline(m.id)]
    assert not any(t.startswith("Compensation") or t == "NodeCompensated" for t in types)
    out = m.outcome
    assert out["failure_policy"] == "PRESERVE_ONLY" and out["rolled_back"] is False
    assert out["partially_completed"] is True
    assert [r["capability"] for r in out["retained_effects"]] == ["sky.launch"]
    assert [c["capability"] for c in out["completed_steps"]] == ["sky.check", "sky.optimize", "sky.launch"]
    assert [i["capability"] for i in out["incomplete_steps"]] == ["deploy.verify"]
    costs = out["retained_costs"]
    (res,) = costs["resources"]
    assert res["capability"] == "sky.launch" and res["resource"]["cluster"] == "sk-app"
    assert res["action_to_stop_cost"] == "sky.down"
    # in the kernel template flow no ranked estimate is threaded into sky.launch, so the cost is EXPLICITLY unknown
    assert res["cost_known"] is False and res["estimated_hourly_usd"] is None
    assert costs["estimated_hourly_usd"] is None and costs["complete"] is False
    assert costs["unknown_cost_resources"] == [res["node_id"]]
    item = [i for i in attention_items(rt, m.id) if i["kind"] == "effects_left_in_place"][0]
    assert item["retained_costs"] == costs and item["recovery_actions"]


def test_cost_is_taken_from_the_approved_estimate_when_present_and_summed():
    from agentic_os.mission.compensation_runner import _retained_costs
    rt, m, _, _ = _fail_deploy()
    rt._compensation_evidence = lambda mid, nid: {"result": {"cluster": "c1", "hourly_usd": 0.85,
                                                            "est_monthly_usd": 620.5, "status": "done"}}
    costs = _retained_costs(rt, m, rt._plans[m.id], m.outcome["retained_effects"])
    (res,) = costs["resources"]
    assert res["cost_known"] is True and res["estimated_hourly_usd"] == 0.85 and res["estimated_monthly_usd"] == 620.5
    assert costs["estimated_hourly_usd"] == 0.85 and costs["complete"] is True and not costs["unknown_cost_resources"]


def test_launch_result_carries_the_optimize_estimate_of_the_approved_placement():
    reg, client, _ = _fleet()
    chosen = {"cloud": "GCP", "region": "us-central1", "instance": "g2-standard-4", "hourly_usd": 0.85,
              "est_monthly_usd": 620.5}
    res = client.invoke("sky", "sky.launch", {"spec": {"name": "demo"}, "chosen": chosen,
                                              "_approval": {"approved": True}}, "k1")
    assert res["hourly_usd"] == 0.85 and res["est_monthly_usd"] == 620.5 and "estimate" in res["cost_basis"]
    bare = client.invoke("sky", "sky.launch", {"spec": {"name": "demo2"}, "_approval": {"approved": True}}, "k2")
    assert bare["hourly_usd"] is None and bare["cost_basis"] == ""        # no estimate -> nothing invented


def test_sky_down_runs_only_on_explicit_opt_in_and_still_needs_its_own_approval():
    rt, m, runs, _ = _fail_deploy(failure_policy="COMPENSATE_DECLARED")
    # sky.down is requires_approval: the saga parks it for a human instead of tearing down on its own
    assert _downs(runs) == []
    assert any(i["kind"] == "compensation_approval" for i in attention_items(rt, m.id))
    rt.compensation_intervention(m.id, "approve", actor="admin", reason="tear down",
                                 node_id=next(i["node_id"] for i in attention_items(rt, m.id)
                                              if i["kind"] == "compensation_approval"))
    assert len(_downs(runs)) == 1


def test_restart_resume_does_not_relaunch_the_cluster():
    reg, client, runs = _fleet()
    cc = _Counting(client)
    store = EventStore()
    rt = MissionRuntime(reg, Executor(cc), store=store)
    m = rt.create_mission("place", policy_refs=["*"], template="sky_deploy")
    rt.run(m.id)                                                        # parks on the launch gate
    rt2 = MissionRuntime(reg, Executor(cc), store=store)
    rt2.rehydrate(m.id)
    m2 = rt2._missions[m.id]
    while m2.state == MissionState.WAITING_HUMAN:
        rt2.approve(m.id, rt2.repo.pending_human(m.id)["node_id"], "approve")
    assert m2.state == MissionState.FAILED
    n = dict(cc.counts)
    rt2.run(m.id)
    rt2.run(m.id)
    assert cc.counts == n and n["sky.launch"] == 1 and n["sky.check"] == 1 and n["sky.optimize"] == 1
    assert len([r for r in runs if r[:2] == ["sky", "launch"] and "--dryrun" not in r]) == 1


def test_needs_you_item_states_unknown_cost_in_a_human_readable_line():
    rt, m, _, _ = _fail_deploy()
    item = [i for i in attention_items(rt, m.id) if i["kind"] == "effects_left_in_place"][0]
    assert "COST UNKNOWN" in item["cost_summary"] and "sky.launch" in item["cost_summary"]
    assert "cluster=sk-app" in item["cost_summary"] and "sky.down" in item["cost_summary"]


def test_estimate_is_withheld_after_failover_to_another_cloud():
    chosen = {"cloud": "GCP", "region": "r", "instance": "i", "hourly_usd": 0.85, "est_monthly_usd": 620.5}
    reg, client, _ = _fleet(launch=(0, "Launching on AWS g5.xlarge\nfailed over from GCP\n", ""))
    res = client.invoke("sky", "sky.launch", {"spec": {"name": "d"}, "chosen": chosen, "_approval": {"approved": True}}, "k")
    assert res["hourly_usd"] is None and res["cost_basis"] == ""
    from agentic_os.mission.compensation_runner import _retained_costs
    rt, m, _, _ = _fail_deploy()
    rt._compensation_evidence = lambda mid, nid: {"result": {"cluster": "c1", "hourly_usd": 0.85, "failed_over": True}}
    (r,) = _retained_costs(rt, m, rt._plans[m.id], m.outcome["retained_effects"])["resources"]
    assert r["cost_known"] is False and "failed over" in r["cost_source"]


def test_a_failed_launch_is_reported_as_possibly_provisioned_with_unknown_cost():
    rt, m, runs, _ = _fail_deploy({"launch": (1, "partial", "quota exceeded after creating head node")})
    assert m.state == MissionState.FAILED and _downs(runs) == []
    (r,) = m.outcome["retained_costs"]["resources"]
    assert r["capability"] == "sky.launch" and r["possibly_provisioned"] is True and r["cost_known"] is False
    assert "POSSIBLY provisioned" in m.outcome["retained_costs"]["summary"]
    item = [i for i in attention_items(rt, m.id) if i["kind"] == "effects_left_in_place"][0]
    assert "POSSIBLY" in item["cost_summary"]
