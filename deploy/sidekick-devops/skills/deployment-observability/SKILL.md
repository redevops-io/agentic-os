---
name: deployment-observability
description: Set up logging, monitoring, dashboards, alerts, SLOs, tracing, or observability for a deployment — and decide whether it needs LIVE, ongoing monitoring after the deploy completes. Use when the user asks to add logging / metrics / monitoring / observability / dashboards / alerts / SLOs / tracing to a service, to watch a deployed stack's logs or performance in real time, or to set up continuous operation. ALWAYS first ask whether they need live monitoring afterwards (not just one-time setup); if yes, follow the continuous-run routine. Missions are episodic, so monitoring is a standing loop + response missions — never a never-ending mission. Governed: irreversible or cost-committing changes gate on a human.
license: Apache-2.0
version: 0.1.0
---

# deployment-observability

Handle any request for logging / monitoring / dashboards / alerts / SLOs / tracing on a deployment. The
non-obvious part: **a deploy mission ends, but the stack keeps running** — observability and load-response are
*continuous* while missions are *episodic*. So don't wrap "watch forever" in a mission; use the three-primitive
model (full architecture: `CR-enterprise/docs/sidekick-mission-supervisor-ops.md` §21).

## The rule — ask first

When the user asks for logging / monitoring / observability, **first ask whether they need LIVE monitoring
*afterwards*** — ongoing observation of the running stack — or just one-time setup:

> "Do you want live monitoring of this stack after it's deployed (dashboards, alerts, ongoing log/metrics
> viewing), or just the observability wired in as part of the deploy?"

- **If NO (one-time):** wire the observability as a **deploy output** and hand back the "where to look" — log
  shipping (Loki), traces (OTEL/Langfuse), dashboards + SLOs (Control Tower), health probes. Done.
- **If YES (live/ongoing):** follow the **continuous-run routine** below.

## The continuous-run routine

1. **Provision observability as a deploy output.** Fold dashboards, log/trace pipelines, **SLOs + error-budget
   alerts**, health probes, and the **autoscaler config** into the `deploy_app` mission's configure/verify steps
   — so they stand up with the app and run autonomously.
2. **Register the standing monitor loop** — NOT a mission. A continuous control loop watches SLIs, error budgets,
   drift, cost, and load. It detects and decides; it does not act. (Maps to drift §5, observability §8, doctor §18.)
3. **Wire the response missions** the loop triggers:
   - **Scheduled:** `cost_audit` (weekly), drift-check (daily), a doctor-poll `monitor` mission, SBOM/vuln per build.
   - **Event-triggered:** SLO burn → remediation · CVE → patch · drift → reconcile · capacity alert → scale-policy ·
     new version → another `deploy_app`.
4. **Set scaling on the right loop:**
   - **Reactive (load spikes) → the platform** (HPA/VPA/Karpenter / cloud autoscaler), configured by the deploy —
     never a mission in the hot path.
   - **Structural (raise ceilings, add a node pool, bigger class, reserved capacity) → a governed mission** — it's
     a rightsizing/commitment finding in `cost_audit`, gated because it commits cost.
5. **Surface it in the cockpit** — real-time logs/perf via the **read→MCP** path (Grafana PromQL/LogQL, Loki, OTEL).
   Note the current limit: live metrics-server reads work; a live Grafana embed needs the observability MCP pointed
   at a real endpoint (config-hook stub otherwise).

## Hard rules

- **Ask about live monitoring before building** — the answer changes everything after step 1.
- **No never-ending missions.** Continuous = the monitor loop; action = short, gated, replayable missions.
- **Reactive scaling is the platform's job**, configured by the deploy; only *structural* scaling is a mission.
- **Gate the cost-committing / irreversible** (reserved capacity, prod rightsizing, node pools) on a human.

## See also

- Architecture + status: `sidekick-mission-supervisor-ops.md` §21 · `sidekick-capabilities.md` (CR-enterprise).
- Companion skills: `deployment-audit` (the audit dimensions incl. rightsizing + SLOs), `deployment-stack-selection`.
- Reads → MCP, mutate → operator: `docs/sidekick-mcp-tooling.md`.
