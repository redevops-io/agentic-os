# deployment-skypilot — REFERENCE

Deeper lookup behind `SKILL.md`. SkyPilot is the open-source multi-cloud runtime; the `sky` operator
(`apps/sky/operator.py`) wraps its CLI as governed capabilities. Verify exact `sky` flags against the
SkyPilot docs before a binding run — flags evolve.

## §A — The workload spec

The spec passed to `sky.optimize` / `sky.launch` (also the mission input for `sky_deploy`):

| field | meaning | example |
|---|---|---|
| `name` | cluster/service name | `llama-infer` |
| `gpus` | accelerator type:count | `L4:1`, `A100:8`, `H100:1` |
| `cpus` / `memory` | min vCPUs / GB (SkyPilot accepts `4+`) | `8+` / `32+` |
| `spot` | use the managed spot market | `true` |
| `cloud` / `region` | **optional** pin — omit to optimize across all enabled clouds | `aws` / `us-east-1` |
| `task_yaml` | a SkyPilot task file (setup/run/resources/service) | `serve.yaml` |
| `run` | inline run command (alternative to `task_yaml`) | `python train.py` |

Leaving `cloud`/`region` unset is the point — the optimizer ranks every enabled cloud.

## §B — The optimizer table (evidence)

`sky launch --dryrun` prints a "Considered resources" table; `core.parse_optimizer` turns it into
ranked candidate dicts (`cloud`, `instance`, `vcpus`, `memory_gb`, `accelerators`, `region`,
`hourly_usd`, `est_monthly_usd`, `chosen`). The chosen row (SkyPilot's ✔) is the default; the whole
ranked list is attached as **mission evidence** at the approval gate, so the human approves a real
cost, not a guess. `est_monthly_usd = hourly × 730`.

## §C — Capability map (the `sky` operator)

| capability | `sky` command | gate / saga |
|---|---|---|
| `sky.check` | `sky check` | read-only; evidence of enabled clouds |
| `sky.optimize` | `sky launch --dryrun` | read-only; ranked table = gate evidence |
| `sky.launch` | `sky launch` | **approval_required**; `undo = sky.down` |
| `sky.serve` | `sky serve up` | **approval_required**; `undo = sky.serve_down` |
| `sky.down` | `sky down` | saga compensation **and** gated idle-teardown |
| `sky.serve_down` | `sky serve down` | saga compensation for serve |
| `sky.status` | `sky status` / `sky serve status` | read-only; the monitor's utilization/idle read |

## §D — Failover, spot, autostop (the SkyPilot economics)

- **Failover** — on capacity/quota error `sky.launch` retries the next-best candidate across regions and
  clouds automatically. The mission need not encode the fallback list; SkyPilot walks its optimizer order.
- **Managed spot** — `--use-spot` runs on the spot market; SkyPilot auto-recovers on preemption
  (relaunch + resume). Preemption rate is part of the measured outcome (§F).
- **Autostop / autodown** — SkyPilot can auto-stop idle clusters (`sky autostop`). Under Sidekick this is a
  **gated** `sky.down` mission (never silent) proposed by the monitor when utilization stays low.

## §E — SkyServe (serving)

`sky serve up` stands up a service with replicas, a load balancer, readiness probes, and replica
autoscaling — optionally spread across clouds/regions. Gate the scale-up cost like any side-effecting
step; `sky.serve_down` is the saga compensation. Define replicas/autoscaling/readiness in the `service:`
block of the task YAML.

## §F — Self-learning: the measured-outcome reward

`sky.launch` returns the **measured** placement outcome — real cost, the cloud/region that actually had
capacity, spot preemption, time-to-ready. Recorded as the reward the placement optimizer learns from, this
is the deployment analog of the v4 measured-cost loop: the runtime drifts toward the config that *delivers*
for a given workload shape, not merely the cheapest estimate. (Reward wiring into the cost model is the
follow-up beyond this first cut, which records the outcome as evidence.)

## §G — SkyPilot vs Terraform/Ansible — when to use which

- **SkyPilot** — *where* a compute workload runs across clouds: GPUs, spot, cheapest-available, ephemeral or
  autoscaled jobs. Best when the answer is "wherever it's cheap/available."
- **Terraform + Ansible** (`infra` operator, per-cloud runbooks) — durable, pinned infra: VPCs, DNS, managed
  DBs, registries, long-lived app stacks on a chosen provider.
- They compose: SkyPilot for the elastic compute tier, Terraform/Ansible for the substrate around it.

## Sources

SkyPilot docs (skypilot.readthedocs.io) — `sky launch`, `sky serve`, `sky check`, `--dryrun`, `--use-spot`,
autostop, the optimizer. Verify flag names against the installed SkyPilot version before a binding run.
