---
name: deployment-skypilot
description: Deploy to the cheapest available cloud/GPU as a governed mission — SkyPilot picks the cloud/region/instance, fails over across providers on capacity, runs managed spot, and autostops idle clusters, all driven by the `sky` operator (sky.optimize → approval gate → sky.launch → verify, saga = sky.down). Use when the target is "wherever it is cheapest/available" rather than a pinned cloud, for GPU/AI or spot-friendly workloads.
license: Apache-2.0
version: 0.1.0
---

# deployment-skypilot

The runbook for deploying a workload with **SkyPilot** through the `sky` operator, as a governed
mission. Unlike the per-cloud runbooks (which pin a provider), this one **lets the optimizer choose
where to run** — the cheapest cloud/region/instance that satisfies the spec, with cross-cloud
failover, managed spot, and autostop. It is written to be followed **literally** by a non-frontier
driver (KIMI/Qwen) — no tool-calling required; emit each command verbatim and **stop at the approval
gate** (never auto-approve `sky.launch`).

## When to use

- The target is **"cheapest available,"** not a pinned cloud — GPU/AI training/inference, batch, or any spot-friendly job.
- You want **cross-cloud failover** (find GPUs wherever they exist) and **managed spot** with auto-recovery.
- If the stack must be a specific provider, use that provider's runbook (`deployment-aws`, `-gcp`, `-azure`, `-digitalocean`, `-proxmox`) instead — this skill executes the *multi-cloud placement*, not a pinned deploy.

## Prerequisites (deterministic checklist — all before step 1)

1. **SkyPilot installed** on the runner (`pip install "skypilot[aws,gcp,azure,kubernetes]"`), `sky --version` works.
2. **Clouds enabled** — `sky check` shows at least one cloud `enabled` (creds present). This is `sky.check`.
3. **A workload spec** — the resource shape: `gpus` (e.g. `L4:1`, `A100:8`), optional `cpus`/`memory`, `spot: true|false`, and either a `task_yaml` (a SkyPilot task file) or a `run` command. See `REFERENCE.md` §A.
4. **The `sky` operator wired** — `build_sky_operator(run=…)` with a real command runner (the bundle ships it dry-run-friendly). Region/cloud are **left unpinned** so the optimizer ranks across all enabled clouds.

## The deployment procedure (a gated mission)

Run in this exact order. Each step is one `sky`-operator capability.

1. **Preflight** — `sky.check`. Confirm the candidate clouds are enabled; the enabled list is evidence.
   ```
   sky check
   ```
2. **Optimize** — `sky.optimize`. Rank cloud/region/instance/spot by **live price + availability**. The
   ranked table **is the evidence** presented at the approval gate — do not skip it.
   ```
   sky launch --dryrun --yes -c <name> --gpus <TYPE:N> [--use-spot]      # prints the "Considered resources" table
   ```
3. **Approval gate (MANDATORY).** Present the ranked table + the chosen candidate's projected cost. `sky.launch`
   is the highest-consequence step — **STOP and wait for human sign-off.** Never auto-approve.
4. **Launch** — `sky.launch`. Provision the chosen candidate. SkyPilot **fails over** across regions/clouds on
   capacity/quota. Saga undo = `sky.down`.
   ```
   sky launch --yes -c <name> --gpus <TYPE:N> [--use-spot] [<task.yaml> | -- "<run cmd>"]
   ```
5. **Verify** — health-check the launched workload's endpoint (the `infra.verify` capability, or `sky.status`).
   ```
   sky status
   ```
   A verify failure **unwinds via the saga**: `sky.down <name>` tears the cluster down.

**Serving instead of a one-shot cluster?** Use `sky.serve` (SkyServe: replicas + autoscaling + load
balancing + readiness), also behind the approval gate, with saga undo = `sky.serve_down`:
```
sky serve up --yes -n <name> <service.yaml>
```

**Idle cost.** `sky.down` doubles as **gated idle-teardown / autostop**: when the monitor reports a cluster
idle past threshold, it proposes a `sky.down` mission for one-click sign-off — never silent.

## Why this fits the OS — the self-learning loop

SkyPilot's optimizer is a one-shot **estimate**. Under the mission runtime, the **measured outcome** of
each `sky.launch` — real cost, whether the region had capacity, how often spot was preempted, time-to-ready
— is recorded as the reward the placement optimizer learns from (the same measured-cost loop the decision
engine already runs). So over time the runtime learns **which cloud/region/spot config actually delivers for
this kind of job**, not just the cheapest sticker price.

## KIMI-reliability note

Follow the steps literally. Emit each `sky` command verbatim. Do **not** pin `--cloud`/`--region` unless the
spec asks — an unpinned launch is what lets the optimizer rank across clouds. **Always run step 2 (optimize)
before step 4 (launch)**, and **stop at the approval gate** — `sky.launch` and `sky.serve` require human
sign-off on the ranked cost. On any launch failure, the saga runs `sky.down`; do not retry blindly.

## See also

`deployment-stack-selection` (whether SkyPilot/multi-cloud is even the right tier) · the per-provider runbooks
(pinned deploys) · `deployment-observability` · `REFERENCE.md` (spec schema, optimizer table, spot/autostop economics).
