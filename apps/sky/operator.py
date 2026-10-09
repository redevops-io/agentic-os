"""SkyPilot as a governed capability provider — the `sky` operator.

  sky.optimize   rank {cloud × region × instance × spot} by live price + availability (evidence)
  sky.launch     provision the chosen candidate        [approval gate]  undo=sky.down (saga)
  sky.serve      autoscaled service (SkyServe)          [approval gate]  undo=sky.serve_down
  sky.down       tear down / idle-teardown (autostop) — also the saga compensation for launch
  sky.serve_down tear down a service — the saga compensation for serve
  sky.status     cluster/service status (the monitor's utilization/idle read)
  sky.check      per-cloud preflight (creds/quota enabled) — evidence before a launch

`build_sky_operator(run=…)` injects the command runner so a deployment wires the real `sky` CLI;
the default calls the CLI directly. It slots beside the `infra` (Terraform + Ansible) operator, so
a placement decision is an ordinary mission — gated, evidence-backed, and rolled back by the saga.
"""
from __future__ import annotations

from agentic_os.mission.operator_sdk import Operator, capability, pending_approval

from . import core
from .learn import PlacementLedger


def build_sky_operator(*, run=None, ledger: "PlacementLedger | None" = None) -> Operator:
    run = run or core._run
    ledger = ledger if ledger is not None else PlacementLedger()

    def _optimize(i):
        spec = i.get("spec") or i
        res = core.optimize(spec, run=run)
        if res.get("candidates"):  # re-rank SkyPilot's candidates by the learned placement reward
            res["candidates"] = ledger.rerank(spec, res["candidates"])
            res["chosen"] = res["candidates"][0]
            res["learned"] = any(c.get("learned_reward") is not None for c in res["candidates"])
        return res

    def _launch(i):
        spec = i.get("spec") or i
        if not i.get("_approval"):           # consequential: launch a real cluster only on a runtime approval
            return pending_approval("sky.launch", chosen=i.get("chosen") or spec.get("chosen"),
                                    cluster_launched=False)
        chosen = i.get("chosen") or spec.get("chosen")
        if chosen:
            spec = {**spec, "chosen": chosen}          # PIN launch to the approved/ranked placement
        res = core.launch(spec, run=run)
        outcome = {"launched": res.get("status") == "done",
                   "had_capacity": not res.get("failed_over", False),
                   "preemption_rate": float(res.get("preemption_rate", 0.0) or 0.0),
                   "time_to_ready_s": res.get("time_to_ready_s", 0)}
        # Attribute the reward to the placement that ACTUALLY launched (from the launch result), not merely
        # the one that was intended — so a capacity failover credits the REAL placement, not a candidate
        # that never ran. Because launch is now pinned to `chosen`, the two coincide unless a failover moved
        # it, which is exactly when the distinction matters.
        actual = {"cloud": res.get("cloud") or (chosen or {}).get("cloud") or spec.get("cloud"),
                  "region": res.get("region") or (chosen or {}).get("region") or spec.get("region", "?"),
                  "instance": res.get("instance") or (chosen or {}).get("instance", "?")}
        res["outcome"] = outcome
        res["reward"] = ledger.record(spec, actual, outcome)  # feed the measured outcome back
        return res

    def _serve(i):
        return core.serve(i.get("spec") or i, run=run)

    def _down(i):
        return core.down(i.get("target") or i.get("cluster") or "sk-app", serve=bool(i.get("serve")), run=run)

    def _serve_down(i):
        return core.down(i.get("target") or i.get("service") or "sk-svc", serve=True, run=run)

    def _status(i):
        return core.status(serve=bool(i.get("serve")), run=run)

    def _check(i):
        return core.check(run=run)

    return Operator("sky", [
        capability("sky.optimize", _optimize, provides=["placement_ranked"],
                   permissions=["infra:read"], estimated_value="medium", latency_ms=8000,
                   concurrency_mode="read_only"),
        capability("sky.launch", _launch, provides=["cluster_launched"],
                   side_effecting=True, approval_required=True, undo="sky.down",
                   permissions=["infra:write"], estimated_value="high", latency_ms=120000,
                   concurrency_mode="exclusive", concurrency_key="sky:cluster:{cluster}"),
        capability("sky.serve", _serve, provides=["service_up"],
                   side_effecting=True, approval_required=True, undo="sky.serve_down",
                   permissions=["infra:write"], estimated_value="high", latency_ms=120000,
                   concurrency_mode="exclusive", concurrency_key="sky:service:{service}"),
        capability("sky.down", _down, side_effecting=True,
                   permissions=["infra:write"], latency_ms=30000,
                   concurrency_mode="exclusive", concurrency_key="sky:cluster:{cluster}"),
        capability("sky.serve_down", _serve_down, side_effecting=True,
                   permissions=["infra:write"], latency_ms=30000,
                   concurrency_mode="exclusive", concurrency_key="sky:service:{service}"),
        capability("sky.status", _status, provides=["sky_status"],
                   permissions=["infra:read"], latency_ms=3000, concurrency_mode="read_only"),
        capability("sky.check", _check, provides=["clouds_enabled"],
                   permissions=["infra:read"], latency_ms=5000, concurrency_mode="read_only"),
    ])
