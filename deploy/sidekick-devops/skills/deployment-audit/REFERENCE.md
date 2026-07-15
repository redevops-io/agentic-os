# deployment-audit — REFERENCE

Level-3 detail for the `deployment-audit` skill. Grounded in 2024–2026 sources (§D). Run dimensions in
the order shown; each de-risks the next.

## §A — Ordered dimension table

| # | Dimension | Signals to check | Tools / method | Typical fixes | Quantify | Auto vs GATE |
|---|---|---|---|---|---|---|
| 1 | Visibility / allocation (FinOps *Inform*) | Tagging <90%; untagged spend; no cost-per-service | FOCUS-normalized billing export (CUR → FOCUS 1.3); unit-cost metrics | Enforce tag policy; map spend to owners | % untagged = $ blind spot; target ≥90% | **Auto** tag backfill; **gate** tag-schema changes |
| 2 | Reliability / SLO | SLI vs SLO; remaining error budget (4-wk); burn rate; missing SLOs on critical paths | Google SRE: SLIs (latency/availability/error), targets, burn-rate alerts | Define/adjust SLOs; burn-rate alerts; freeze on exhaustion | Error budget = change-risk allowance; downtime $/min | **Auto** alerts/dashboards; **gate** SLO target changes & freezes |
| 3 | Cost: idle & orphaned (zero-risk) | Stopped-but-billing; orphaned disks/EIPs/snapshots; idle LBs; 24/7 non-prod | Cost Explorer / cost tool; usage=0 over N days (~29% of spend is this) | Delete orphans (snapshot-first); **schedule non-prod off** nights/weekends | Resource $/mo; non-prod scheduling 60–70%; orphan cleanup $50–150K | **Auto** non-prod schedule + unattached-IP release; **gate** volume/snapshot delete |
| 4 | Performance profiling | Slow endpoints; p95/p99; which span then which function | APM + distributed tracing (OTel; Profiles signal → flame graph for the span) | Fix hot function; cache; async/batch; remove blocking calls | Latency → conversion/SLO $; CPU-seconds saved × $ | **Gate** code changes; **auto** config-level (cache header, pool size) w/ rollback |
| 5 | Database / query | **N+1** ("300 queries/page"); missing index; `SELECT *`; **rows examined ≫ returned**; Seq Scan | `pg_stat_statements` / MySQL slow-query log → leaderboard → **`EXPLAIN ANALYZE`** >100ms | Add index; eager-load/batch; rewrite; cache; drop unused indexes | Query total_time × calls; 800ms→3ms × concurrency; DB CPU $ | **Gate** all DDL (dry-run + EXPLAIN in PR); **auto** may *propose* index + impact |
| 6 | Compute rightsizing (VMs & k8s) | Requests ≫ actual; low pod density; sized for theoretical peak; util <30–40% | VPA recommender; actual-usage percentiles; Karpenter/CA consolidation | Lower requests/limits to p95+headroom; consolidate nodes; Karpenter | Node-count × node $; 20–35% compute, up to ~80% via bin-packing | **Gate** prod request/limit (restart risk, stateful); **auto** non-prod w/ rollback |
| 7 | Commitments (AFTER #6) | On-demand coverage of stable baseline; coverage <70–80%; expiring RIs/SPs | Analyze 90-day steady state; target 70–80% of baseline | Buy Savings Plans/RIs on rightsized baseline; ladder terms | RI/SP 40–72% vs on-demand | **GATE always** (1–3yr financial, hard to reverse) |
| 8 | Config / infra drift | Live state ≠ IaC; out-of-band changes (86% of orgs report drift) | `terraform plan` / driftctl daily | Reconcile: codify or revert; policy-as-code | Drift ≈ 40% of infra time on manual remediation; outage/security risk | **Gate** apply/revert; **auto** detection + PR proposal |
| 9 | Security & supply chain | Vuln/unmaintained deps (80% >1yr stale); no SBOM; unpinned; over-permissive IAM; public buckets | Syft/Trivy/CycloneDX SBOM in CI + VEX; SLSA L3+ (hash-pinned); CSPM | Patch/bump; pin by hash; sign (Sigstore); tighten IAM | Breach cost / CVSS × exploitability (VEX) | **Auto** isolated patch bumps behind green tests; **GATE** IAM/public-exposure/major bumps |
| 10 | Data-pipeline efficiency | Full-scan jobs that should be incremental; redundant recompute; unpartitioned scans; oversized clusters | Warehouse query/cost logs; bytes-scanned per job | Partition/cluster; incremental models; prune columns; right-size warehouse; cache | Bytes-scanned × $/TB; cluster-hours saved | **Gate** pipeline logic; **auto** warehouse auto-suspend/size non-prod |
| 11 | ML-specific | GPU util (~30% typical); training/retrain cost; feature/data drift; idle serving endpoints | DCGM/GPU metrics; MIG/fractional GPU; drift detectors; serving batching | MIG/fractional GPUs; batch inference; autoscale-to-zero; drift-triggered retrain | GPU-hrs × $ (H100 ≈ $6/GPU/hr); retrain ≈ 15–25% of initial/cycle | **Gate** retrain triggers & serving changes; **auto** scale idle→zero non-prod |

## §B — Definition of done

Coverage (every dimension run or N/A-with-reason; allocation ≥90%) · every finding quantified ($/mo or
risk score + confidence + reversibility; **no unpriced findings**) · every finding has a proposed fix with
a dry-run/EXPLAIN/`terraform plan` preview + rollback path · auto/gate classification applied · SLO context
attached and no auto-change on an exhausted error budget · auto-applied changes verified post-hoc (health
green, SLIs unchanged, rollback confirmed) · prioritized report ranked by $/risk × confidence with the
top-N first + total identified vs realized savings · idempotent & scheduled (no false-new findings for
already-fixed items).

## §C — Continuous / automated audit design

Scheduled + event-driven, not one-off: drift detection ≥ daily; cost/idle scans daily; SBOM + vuln scan on
every CI build (living SBOMs + VEX, not static); profiling continuous (OTel Profiles as the 4th signal).
**Tiered autonomy ("sandwich"):** hard regulatory/policy rails on the floor, agent reasons in the middle,
human ceiling on anything irreversible or externally visible. Confidence-gated: below threshold pauses for
review; every sensitive change ships with dry-run + health check + rollback.

## §D — Sources

FinOps/FOCUS: focus.finops.org/focus-specification (v1.3 Dec 2025) + what-is-focus; prosperops finops-framework; finops.org/wg/how-to-optimize-cloud-usage; byteiota finops-2026-implementation-guide; binadox underutilized-ebs; usage.ai cloud-cost-best-practices.
Well-Architected: docs.aws.amazon.com/wellarchitected cost-optimization-pillar + performance-efficiency-pillar.
SRE/DORA: sre.google/workbook error-budget-policy + implementing-slos + alerting-on-slos; sre.google/sre-book service-level-objectives.
DB/query: medium jholt1055 DBA-slow-query-2025; howtocenterdiv your-database-is-the-bottleneck; blog.vibecoder.me debugging-database-queries; medium x0goe mysql-slow-query-log.
Perf/observability: oneuptime connect-otel-profiles-traces; signoz flamegraphs; uptrace distributed-tracing.
K8s rightsizing: scaleops kubernetes-workload-rightsizing; sedai k8s-capacity-planning + kubernetes-autoscaling.
Drift: spacelift terraform-drift-detection; env0 ultimate-guide-terraform-drift.
Security/supply-chain: oligo.security supply-chain-2025; cloudsmith 2026-guide-supply-chain; OWASP software-supply-chain cheat sheet; cisa.gov SBOM.
ML/GPU: youngju.dev mlops-complete-guide-2025; bhavishyapandit9 mlops-gpu-cost.
Auto-apply vs gate: tamnoon agentic-cloud-remediation; bigid agentic-remediation-guide; rack2cloud ai-policy-agents-guardrails-2026.
