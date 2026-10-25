# deployment-stack-selection — REFERENCE

Level-3 detail for the `deployment-stack-selection` skill. Loaded only when the recommendation needs
the exact matrix/thresholds. Grounded in 2024–2026 sources (§F). Tiers: **T1** PaaS/serverless-app ·
**T2** managed cloud · **T3** self-hosted · **T4** serverless-ML · **T5** managed/rented-ML.

## §A — Decision matrix (workload × criteria → tier)

| Workload | Default tier | Flip to (when) | Ruled out |
|---|---|---|---|
| Stateless web / API | T1 PaaS | T2 at >4–5 services or >$500/mo; T3 at steady $1M+/yr | k8s if this is *all* you run |
| Stateful DB | T2 managed DB | T3 self-managed only with a dedicated DBA/infra owner | Pure serverless (no persistent identity) |
| Batch / ML training | T4 (Modal) jobs; T5 rented cluster for large runs | T5 reserved cluster / SageMaker HyperPod at 100B+ params, multi-week | Always-on dedicated GPU (idle burn) |
| Real-time ML inference | T4 serverless GPU if bursty | T5 dedicated/reserved above ~50% GPU util (~12–15 h/day) | 24/7 serverless at high QPS (2–4× premium) |
| Streaming / data pipeline | T2 (stateful stream engine, Kafka+Flink) | T3 if data-gravity/egress dominates at scale | Cron/Airflow if you need event-time; serverless for stateful streams |
| Cron / scheduled jobs | T1 serverless (or T4 Modal for GPU) | T2 container if >50k invocations/day or >50% duty | Always-on instance for a 2-min/day job (80× overpay) |
| Edge / low-latency global | T1 Fly.io (full container) or Vercel (edge fns) | T3/T2 multi-region for persistent conns + local DB | Single-region SSR when SLO can't absorb ~200ms cross-continent |

## §A2 — AWS execution environments for agents and long-running work (candidates, not defaults)

Applies when the target stack is AWS and the workload is an agent, a long-running task or a workflow. "Lambda" is no longer one
thing: the 15-minute stateless function is only the On-Demand form. The rows below name a **candidate**; they are not deployment rules,
and **nothing here changes a default**: no Lambda form is preferred over the existing k3s/Proxmox or ECS/EKS paths until it has been
measured (a bounded spike, then a benchmark against the current runner).

| Workload | Preferred candidate | What disqualifies it |
|---|---|---|
| Short, stateless invocation | Ordinary Lambda (Event Functions, On-Demand) | 15 min per invocation; no retained state |
| Long-running asynchronous task | Lambda Managed Instances | 90 min and only for async / event-source-mapping invocations (synchronous stays 15 min); EC2-backed, so it pays off for continuously utilized load (peak-to-mean < 2), not bursty; > 90 min -> containers / EC2 |
| Durable orchestration with long waits | Lambda Durable Functions | each step is bounded by its capacity provider's timeout; supported runtimes only (Python 3.13+, Node.js 22+, Java 17+, .NET 8+); does not replace the Mission Runtime's approvals, receipts and replay |
| Isolated agent code with resumable process state | Lambda MicroVMs | **ARM64 only; no GPU; 8-hour session lifetime, and suspended time counts toward it; egress is the open internet unless a VPC connector restricts it**; Dockerfile-in-a-zip-in-S3 image on an AWS base image; state must outlive the session outside it; Terraform / CloudFormation support unconfirmed |
| Managed agent hosting | AgentCore Runtime | you do not control the VM lifecycle; identity, MCP and A2A are AWS's |
| Persistent services, specialized hardware or GPU | ECS, EKS or EC2 | see `deployment-aws` §A-§B |

**Evaluate before choosing** (in this order): duration against the form's ceiling · architecture (ARM64 clean?) · network egress and
destinations · region availability · quotas · cost model (per-second vCPU and GB; snapshot write / read / storage; suspended time is not
billed as compute) · security (who holds credentials, which side is the controller) · lifecycle guarantees (what happens at expiry, on VM
loss, on a failed resume).

**If a MicroVM is chosen:**
- **Mission lifetime is independent of VM lifetime.** A mission may stay parked for days; a MicroVM cannot (8 h including suspended time).
  Suspend for short approval waits when economical; for long waits checkpoint the durable mission state outside the VM, terminate it, and
  launch a new session after approval. One mission may therefore use several MicroVM sessions; session-per-mission is a starting
  convenience, not an invariant.
- **Keep the controller outside the sandbox.** The Mission Runtime stays authoritative for approvals and separation of duty, execution
  receipts and replay protection, sandbox credentials and capability grants, network policy, recovery and retry, budgets and termination.
  The agent process must not keep its own approval state, enforce its own permissions, or declare its own execution successful.
- **Snapshot hygiene.** The image is snapshotted after init, so unique content (IDs, secrets, connections) created at build time is shared by
  every MicroVM from that image version: create it in the `/run` hook and re-establish connections on resume.
- **Deny the network with a VPC egress connector, not by omission, and attach no execution role unless the code needs AWS access** (its credentials are readable by guest code).
- **Record the image identity with every session** for provenance: version, base image version and artifact hashes (MicroVM images have no registry digest).

**Confirmed vs measured vs unverified** (as of 2026-10-25; re-check before relying on any number):
- *Confirmed in AWS documentation / posts:* MicroVMs are ARM64-only; up to 8 hours of total runtime; up to 16 vCPU, 32 GB memory, 32 GB disk;
  memory and disk preserved across suspend / resume; suspended MicroVMs accrue no compute charge (snapshot charges still apply); image
  built from a Dockerfile zip in S3 and snapshotted; lifecycle `run / suspend / resume / terminate` through `aws lambda-microvms`; public egress
  by default, VPC egress through a network connector; available in 14 Regions after the October 2026 expansion. Managed Instances: 90 minutes for
  async invocations, Graviton5 instance types supported. Durable Functions: executions can wait up to a year at no compute cost.
- *Measured on a real account* (us-east-1, one image, single runs; spike and raw evidence in `runtime-plugins-enterprise` `spikes/microvm`):
  - **Default egress is the open internet, and omitting the egress connector does not deny it**: the service applies `INTERNET_EGRESS` and guest
    code reached the internet. AWS documents no deny-all connector. A customer-managed VPC egress connector in an isolated subnet whose security
    group has no outbound rules blocked TCP, but **DNS resolution still worked** (an exfiltration channel). A "no network" requirement therefore needs
    that connector; do not infer it from leaving the parameter out.
  - **An execution role's temporary credentials are readable by any code in the VM** through the metadata endpoint; with no role the VM has no AWS
    credentials. Treat a role as a credential handed to untrusted code; the default is no role.
  - `maximumDurationInSeconds` is enforced for running **and suspended** VMs alike (a suspended VM was terminated at the limit); a terminated VM cannot be resumed.
  - Start to `RUNNING` about 3.5 s, suspend and resume about 2.2 s each, first request to a suspended VM with auto-resume 0.65 s; process, memory and
    file survived suspend, the monotonic clock paused while the wall clock jumped.
  - All VMs from one image version share state created at build time (use the `/run` hook for per-VM values). Inbound requests need a per-VM,
    per-port token (missing, foreign and wrong-port tokens were refused). The guest runs as root with elevated capabilities inside the VM.
  - No registry-style image digest exists; provenance is image version + base image version + hashes of the code artifact.
- *Unverified or third-party only:* AgentCore Runtime session and "Runtime Instances" limits; Terraform / CloudFormation resources for MicroVMs
  (none found); the real 8-hour expiry (tested at 2 minutes); behaviour after a genuinely failed resume; host allow-lists through a VPC connector;
  idle-parameter ranges, quotas and image limits; behaviour under load.

## §B — The 12 heuristics (if-then, agent-applicable)

1. No dedicated infra owner (< ~4 platform/DevOps eng) → never self-managed k8s / bare metal; default T1 PaaS. Ops labor (20–40% of an engineer, $2.5–8k/mo) erases hardware savings.
2. < ~10 services AND < ~10 infra eng → PaaS or managed containers (Fargate/Cloud Run), not k8s. k8s justifies itself ~20+ services / multi-region / regulated.
3. PaaS bill < ~$500/mo AND < 4–5 services → stay PaaS. Migrate to T2 when ≥2 of {PaaS outage hit paying customers, bill >$500 & growing, compliance demand, >4–5 services, time lost to platform limits}.
4. Bursty/spiky (low avg util) → serverless/scale-to-zero (Cloud Run, Lambda, Fly auto-stop, Modal). Steady ≥ ~50% duty → always-on reserved.
5. GPU inference, sustained util < ~40% (< ~5–6 h/day) → serverless GPU (T4). > ~50–60% (>~12–15 h/day) → dedicated/reserved (T5). **Governing formula: break-even utilization ≈ 1 − reserved discount.**
6. Handles PHI → require BAA-eligible service + signed BAA (AWS/GCP/Azure all offer one). HIPAA imposes **no** region requirement; rule out non-covered services for PHI.
7. EU personal data → EU region + DPF-certified processor or SCCs. Strict sovereignty/French-health (HDS) → sovereign-cloud/HDS-certified regions only. **Compliance is per-region, not per-provider.**
8. FedRAMP High → segregated env (AWS GovCloud); commercial regions cap at FedRAMP Moderate. SOC2/ISO/PCI generally do NOT force self-hosting.
9. Egress-heavy → flag egress cost first: hyperscaler $0.09–0.20/GB vs Fly $0.02/GB vs Cloudflare R2 $0. Keep compute next to data; prefer zero-egress storage.
10. High-bandwidth prod on premium PaaS (Vercel $0.15/GB post-cut) → warn of bill-shock; a viral 1TB spike ≈ $1,100 overage. Propose Fly.io/self-host.
11. Runs > ~6 months at 70–80%+ steady util → reserved instances / savings plans / CUDs (~40% off, ~6-month all-upfront break-even). Target 70–80% coverage, not 100%.
12. Stateless-clean (12/15-factor: config in env, disposable, attached backing services) → portable across tiers → prefer managed/serverless for velocity. Stateful/long-lived conns/WebSockets → rule out ordinary (On-Demand) serverless functions; use T2/T3, or on AWS check the §A2 candidates (MicroVMs, Managed Instances, Durable Functions) before ruling serverless out containers.

## §C — Cost & scale flip thresholds

**App/compute**
- PaaS → VPS/self-host: solo/small flips ~**$40–50/mo** PaaS bill (Hetzner CX22 ~$4.59 vs Vercel Pro+DB ~$120–200 — 20–40× gap). Covers most apps < ~100k monthly users on 1–2 servers.
- Serverless → always-on container: ~**50k invocations/day** or **>50% duty cycle** (for On-Demand functions; on AWS, Lambda Managed Instances is the Lambda option for continuously utilized load, see §A2). Real case: 40 Lambda fns → ECS Fargate cut bill 73% ($9,400 → $2,500/mo). Inverse: 2-min/day job is 80× cheaper on Lambda.
- Managed cloud → repatriation: ~**$1M+/yr** steady spend, high util + large egress/storage. 37signals: $3.2M → $1.3M/yr (~$2M saved 2024) after ~$700k capex; 18PB Pure Storage < $200k/yr.
- DIY platform / self-run k8s: org break-even ~**$2–2.5M/yr** cloud (or 50+ engineers) — a min viable platform team ≈ 3 seniors ≈ $600k/yr before compute. Managed k8s (EKS/GKE) keeps ~30–50% of DIY savings without running the control plane.

**GPU**
- Serverless → dedicated/reserved: break-even band ~**30–60% util**, canonical ~50% (~12 h/day). Below ~40% serverless is 3–5× cheaper; serverless carries ~2–4× per-active-second premium.
- Rent → own an H100: buy $25–40k; naive break-even ~347 days 24/7 @ $3/hr, realistically **18+ months near-100% util** with hosting/power.
- GPU $/hr (2026 snapshot — tunable, verify live): H100 Modal $3.95 / RunPod $3.35 / Baseten $6.50; Lambda Labs on-demand $3.99–4.29; discount clouds $2.01–2.50, spot ~$1.03. A100 80GB $1.07–5.04. L4 ~$0.43–0.80. T4 ~$0.19–0.63. Hyperscaler on-demand H100 ~$10–12/GPU-hr.
- Fine-tuning < 70B: per-token API (Together LoRA ~$0.48/M tok, Fireworks ~$0.50/M) beats renting GPUs; move to dedicated endpoints only at steady volume.

**Egress cliffs**
- Internet egress: AWS $0.09/GB, Azure $0.087, GCP $0.12 (first ~10TB) → ~$0.05–0.07 above 50–150TB. Hidden intra-cloud: NAT Gateway $0.045/GB, cross-AZ $0.01/GB each way (can add 50–200%). First 100GB/mo free on AWS/Azure; Cloudflare R2 = $0 egress.

**Cold starts (for latency SLOs)**
- Lambda 100ms–2s (Rust ~16ms; Java/C# up to 2s; since Aug 2025 AWS bills the INIT phase, +10–50% for heavy-startup fns). Cloud Run ~1.1s. Fly scale-to-zero ~300ms–2s. Render free 30–60s. GPU: Baseten sub-second, RunPod FlashBoot <250ms, Modal seconds, Replicate custom 30–120s.

## §D — Red flags (stop and ask the human)

Regulated data class (PHI/GDPR/FedRAMP/PCI/residency) · tier flip implying migration cost/capex · reserved/committed spend or GPU ownership · egress/bandwidth exposure · self-managed stateful/data-loss-risk workload without a named owner · latency SLO the default tier can't meet · team-maturity mismatch (< ~4 infra eng or below Google MLOps L1 / MS L2) · vendor lock-in step-change where portability mattered · **utilization unknown/unmeasured**.

## §E — Reference frameworks (ground each call)

Well-Architected (AWS 6 / GCP 6 / Azure 5 pillars) — per-decision trade-off checklist · 12/15-Factor — portability litmus (config-in-env, disposable, attached backing services) · CNCF cloud-native definition v1.1 — "cloud-native vs lift-and-shift" test + Landscape maturity · MLOps maturity (Google 0–2 / MS 0–4) — diagnose before choosing an ML stack · Team Topologies — infra as a platform-as-a-service; cognitive load triggers abstracting behind managed services · DORA four keys — outcome scoreboard (reject safe-but-slow).

## §F — Sources

AWS execution environments (§A2): docs.aws.amazon.com/lambda/latest/dg/microvms-how-it-works.html · aws.amazon.com/blogs/aws/run-isolated-sandboxes-with-full-lifecycle-control-aws-lambda-introduces-microvms · aws.amazon.com/lambda/pricing · aws.amazon.com/about-aws/whats-new/2026/09/aws-lambda-graviton5-ec2 · aws.amazon.com/about-aws/whats-new/2026/10/lambda-microvms-4-additional-regions · infoq.com/news/2026/09/lambda-90-minute-timeout (third-party) · gerardo.dev/en/lambda-microvms-vs-agentcore-runtime (third-party opinion).
Tiers/migration: byteiota "PaaS-first 2026", encore.dev/articles/kubernetes-cost, cloudraft railway-render-flyio→k8s, AWS Fargate-or-Lambda decision guide, umh.app AWS/Azure-vs-Hetzner, theregister/datacenterdynamics 37signals repatriation, deploybase Vercel bill-shock.
Thresholds: devtoolpicks Vercel-vs-Hetzner, byteiota Lambda-vs-containers, egresscost.com, spendark egress guide, hykell/repost AWS savings-plans-vs-RI, cloudzero H100 cost.
ML: gmicloud serverless-vs-dedicated, spheron serverless-vs-on-demand-vs-reserved + gpu-pricing-2026 + fine-tuning-cost, lambda.ai/pricing, modal.com/pricing, towardsdatascience SageMaker-vs-Vertex.
Axes/compliance: sedai serverless-vs-k8s, usavps VPS-vs-serverless-vs-containers, akave egress-fee-trap, opsiocloud GDPR residency, aptible/hipaavault HIPAA residency, secureframe FedRAMP-20x, ai-infra-link when-to-avoid-k8s, duplocloud devops-maturity.
Frameworks: aws.amazon.com/architecture/well-architected, cloud.google.com/architecture/framework, learn.microsoft.com/azure/well-architected, 12factor.net, github.com/cncf/toc DEFINITION.md, landscape.cncf.io, cloud.google.com/architecture/mlops-…, learn.microsoft.com azure mlops-maturity, teamtopologies.com/key-concepts, dora.dev/guides/dora-metrics-four-keys.

*Confidence: utilization break-evens (~50% GPU, ~50% container duty, ~50k invocations/day) and the 37signals figures corroborate across 3+ sources — safe to encode. GPU $/hr and PaaS list prices are vendor/comparison snapshots — encode as tunable config with a "verify against live pricing" flag. Compliance specifics are a mandatory human-confirmation gate.*
