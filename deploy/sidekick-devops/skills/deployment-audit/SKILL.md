---
name: deployment-audit
description: Audit a RUNNING deployment for cost, performance, database/query, reliability, security/supply-chain, config drift, data-pipeline, and ML/GPU waste — find waste and risk, quantify a $ or risk number per finding, propose fixes, auto-apply the safe reversible ones and gate the risky ones on human approval. Use when auditing an existing system, chasing a high or rising cloud bill, a slow page or endpoint, doing cost optimization / FinOps / rightsizing, or setting up a continuous scheduled audit. Encodes the ordered playbook (visibility → SLO → idle → performance → DB → rightsize → commit → drift → security → data → ML) and the reversibility × blast-radius × confidence gate rule. This is the runtime engine behind the cost_audit mission.
license: Apache-2.0
version: 0.1.0
---

# deployment-audit

Read a running system, find the waste and risk, **price every finding**, and split fixes into
auto-apply (safe + reversible) vs gate-on-human (risky/irreversible). This is the DevOps-Sidekick
audit — for the "nobody has read the code in 8 months" problem, where engineering decisions go
un-audited until the stack breaks or the bill gets too high. Meant to run **on a schedule**, not once.

## The ordering principle (why this sequence)

Run dimensions so each stage **de-risks the next** and cheap/reversible wins land before expensive/irreversible ones:

1. **Visibility & allocation first** (FinOps *Inform*) — you can't price what you can't see; get tagging/allocation ≥90%. This is the denominator for every $ estimate.
2. **Reliability / SLO baseline second** — compute the **error budget** (= 1 − SLO) *before* touching anything. Remaining budget is your **risk allowance** that decides what may auto-apply vs must gate. Never auto-apply while the budget is exhausted (respect the release freeze).
3. **Zero-risk cost cleanup third** — idle/orphaned resources: reversible, ownerless, fastest ROI.
4. **Performance → database → rightsizing** — profile to find hotspots, **fix the queries** (usually the biggest single lever), *then* rightsize compute to the now-lower demand.
5. **Commitments AFTER rightsizing** — the one ordering mistake that costs the most money is **buying Reserved Instances / Savings Plans before rightsizing**. Rightsize → then commit to the smaller baseline.
6. **Drift → security/supply-chain → data-pipeline → ML** last, once the system is understood.

## The single highest-yield probe

**Read the queries the app actually runs.** `pg_stat_statements` / MySQL slow-query log → leaderboard by total time → `EXPLAIN ANALYZE` anything > 100ms → look for **rows examined ≫ rows returned**. This catches the N+1 / "300 queries per page" class, and one missing index can turn 800ms → 3ms, compounding under concurrency. Do this before rightsizing — the query fix lowers the demand you'd otherwise size for.

## Top highest-ROI checks (find the most waste fastest)

1. **Non-prod scheduling** — dev/staging off nights & weekends → 60–70% off non-prod. Zero-risk, auto.
2. **Orphaned storage sweep** — unattached disks, stale snapshots, idle LBs/EIPs → typically $50–150K. Snapshot-first, then gate the delete.
3. **Slow-query leaderboard + EXPLAIN** — highest performance-per-hour.
4. **N+1 detection** — trace one page, count queries; eager-load to collapse.
5. **K8s rightsizing (VPA recommender)** — requests set for theoretical peak → drop to observed p95 → 20–35% compute (more via bin-packing).
6. **Idle instances/endpoints** (incl. idle GPU serving) — usage ≈ 0, billing 24/7.
7. **Commitment coverage gap** (after rightsizing) — stable baseline on-demand → Savings Plans/RIs (40–72%). Always gate.
8. **Tag/allocation to ≥90%** — unlocks every $ estimate; finds ownerless spend.
9. **Stale/vulnerable deps + SBOM** — auto-bump isolated patches behind green tests.
10. **Bin-packing / node consolidation** (Karpenter) — collapse low-density nodes.

Full dimension table (signals → tools → fixes → how to quantify → auto/gate) is in `REFERENCE.md` §A.

## The gate function — auto-apply vs human approval

Classify every finding on three axes: **reversibility × blast radius × confidence**.

- **Auto-apply** when reversible + low blast radius + above confidence threshold, and error budget is healthy: non-prod scheduling, orphan cleanup (snapshot-first), tag backfill, isolated patch dep-bumps behind green tests, alert/dashboard creation, non-prod rightsizing.
- **Gate on a human** (ship the evidence — trace / EXPLAIN / `terraform plan` — with the proposal): index/schema DDL, prod request/limit changes, RI/SP commitments, IAM or public-exposure changes, major-version bumps, SLO target changes, pipeline logic changes, ML retrain triggers.
- Rule of thumb: **externally visible, identity-destructive, financially committing, or a schema/IAM change → always gate.** Automation speeds the human decision; it never hides the evidence behind it.

## Definition of done

Every dimension run (or marked N/A with reason); allocation ≥90% · **every finding quantified** ($/mo or risk score) with confidence + reversibility, no unpriced findings · every finding has a proposed fix with a dry-run/EXPLAIN/plan preview + a rollback path · auto/gate classification applied · SLO context attached and no auto-change made on an exhausted budget · auto-applied changes verified post-hoc (health green, SLIs unchanged, rollback confirmed) · a prioritized report ranked by $/risk × confidence with total identified vs realized savings · idempotent & scheduled (re-running produces no false-new findings for fixed items).

## Hard rules

- **Price everything.** A finding with no $ or risk number is not done.
- **Rightsize before you commit.** Never lock a 1–3yr commitment around an oversized footprint.
- **The SLO error budget is the risk allowance** — it decides how aggressive auto-apply may be.
- **Gate the irreversible, ship the evidence.** Every gated proposal carries the trace/EXPLAIN/plan that justifies it.

## See also

- `REFERENCE.md` — full ordered dimension table, DoD checklist, continuous-audit design, cited sources.
- Companion skill: `deployment-stack-selection` (choose where to deploy). Companion mission: `cost_audit` (this skill is its engine; the demo reproduces a real $11k→$1.9k/mo audit). Reads should prefer MCP connectors; guarded writes are custom operators — see `docs/sidekick-mcp-tooling.md`.
