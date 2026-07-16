# `sky` operator — SkyPilot as a governed capability provider

SkyPilot ([skypilot-org/skypilot](https://github.com/skypilot-org/skypilot)) owns **where** a
workload runs — the cheapest available cloud/region/instance, cross-cloud failover, managed spot,
autostop. This operator wraps its CLI as governed Mission-Runtime capabilities, so a placement
decision is an ordinary mission — gated, evidence-backed, saga-rolled-back. It slots beside the
`infra` operator (Terraform substrate + Ansible config); SkyPilot is the elastic-compute tier.

| capability | `sky` command | gate / saga |
|---|---|---|
| `sky.check` | `sky check` | read-only — enabled clouds as evidence |
| `sky.optimize` | `sky launch --dryrun` | read-only — the ranked cost/availability table is the **gate evidence** |
| `sky.launch` | `sky launch` | **approval_required**, `undo = sky.down` |
| `sky.serve` | `sky serve up` | **approval_required**, `undo = sky.serve_down` |
| `sky.down` / `sky.serve_down` | `sky down` / `sky serve down` | saga compensation + gated idle-teardown |
| `sky.status` | `sky status` | read-only — the monitor's utilization read |

`build_sky_operator(run=…)` injects the command runner (wire the real `sky` CLI; the default calls it
directly). The `sky_deploy` mission template runs check → optimize → **[approval]** → launch → verify.

**Self-learning:** `sky.launch` returns the *measured* placement outcome (real cost, capacity,
preemption, time-to-ready) — the reward the placement optimizer learns from, the deployment analog of
the v4 measured-cost loop. See `deploy/sidekick-devops/skills/deployment-skypilot/`.
