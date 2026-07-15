---
name: deployment-stack-selection
description: Recommend WHERE to deploy a workload — PaaS/serverless vs managed cloud vs self-hosted vs serverless-ML vs rented/dedicated GPU — from its workload type, scale, utilization, latency SLO, compliance class, team size, and budget. Use when planning a deployment, choosing a cloud or stack, comparing hosting options, sizing GPU, estimating hosting cost, or when a deploy spec's target stack is unset or looks wrong. Applies grounded 2024–2026 tier heuristics and cost/utilization break-evens, and flags decisions that need human sign-off (compliance, migrations, committed spend). Advisory only — the user's explicit choice always wins.
license: Apache-2.0
version: 0.1.0
---

# deployment-stack-selection

Turn a workload description + a few facts into a **defensible stack recommendation**, with the reasoning and the break-even that drove it. This is what Sidekick runs during preflight / the cost advisor before a `deploy_app` mission. **You advise; the human decides** — surface the recommendation and the runner-up, and let the user's explicit pick stand.

## The five tiers you choose among

- **T1 — PaaS / serverless-app**: Fly.io, Render, Railway, Cloud Run, Vercel, Lambda
- **T2 — Managed cloud**: EKS/ECS/Fargate, GKE, AKS, DigitalOcean (managed containers + managed DB)
- **T3 — Self-hosted**: k3s/Proxmox, Hetzner/OVH dedicated, bare metal
- **T4 — Serverless ML**: Modal, Replicate, Baseten, RunPod serverless (scale-to-zero GPU)
- **T5 — Managed / rented ML**: SageMaker, Vertex AI, Lambda Labs / RunPod pods, dedicated LLM endpoints

## Procedure

1. **Gather the inputs.** Workload type (stateless web/API · stateful DB · batch/ML training · realtime inference · streaming/pipeline · cron · edge); traffic shape (spiky vs steady + duty cycle); latency SLO; **compliance class** (PHI/GDPR/FedRAMP/PCI/residency); team size & ops maturity; budget/burn; GPU need + expected utilization; lock-in tolerance. **If utilization or compliance is unknown, stop and ask** — most flips depend on a real number.
2. **Apply the matrix** (`REFERENCE.md` §A): workload → default tier, and the condition that flips it.
3. **Apply the heuristics** (`REFERENCE.md` §B, 12 if-then rules). The load-bearing ones:
   - No dedicated infra owner (< ~4 platform engineers) → **never** self-managed k8s / bare metal; default T1.
   - < ~10 services and < ~10 infra engineers → PaaS or managed containers, not k8s (k8s earns its keep ~20+ services / multi-region / regulated).
   - Bursty / low duty cycle → serverless/scale-to-zero; steady ≥ ~50% duty cycle → always-on reserved.
   - **GPU: break-even utilization ≈ 1 − reserved discount.** < ~40% util → serverless GPU (T4); > ~50–60% (>~12–15 h/day) → dedicated/reserved (T5).
   - Egress-heavy → price egress FIRST (hyperscaler $0.09–0.20/GB vs Fly $0.02 vs Cloudflare R2 $0); keep compute next to data.
4. **Check the cost/scale flip thresholds** (`REFERENCE.md` §C) — PaaS→VPS ~$40–50/mo solo; serverless→container ~50k invocations/day or >50% duty; managed→repatriation ~$1M+/yr steady; DIY platform/k8s ~$2–2.5M/yr or 50+ engineers.
5. **Run the red-flag check** (`REFERENCE.md` §D). If any fire, present the recommendation but **require human confirmation** — do not auto-proceed.
6. **Recommend**: name the tier + the cheapest viable option in it, state the one break-even that decided it, name the runner-up, and (if a red flag fired) the specific thing you need the human to confirm.

## Red flags → must ask the human (never auto-decide)

Regulated data (PHI/GDPR/FedRAMP/PCI/residency) · a tier flip that implies a migration or capex · reserved/committed spend or buying GPUs · egress/bandwidth bill-shock exposure · self-managed stateful DB with no named DBA · a latency SLO the default tier can't meet · team-maturity mismatch (k8s/self-host with < ~4 infra engineers) · a lock-in step-change where portability was a stated concern · **utilization is unmeasured** (you're guessing the number a flip depends on).

## Hard rules

- **Advisory, not authoritative.** Output a recommendation + runner-up + the deciding break-even. The user's explicit stack choice wins; record it and move on.
- **Show the number.** Every recommendation cites the specific threshold/utilization break-even that drove it (from `REFERENCE.md`), not a vibe.
- **Prices drift; thresholds don't (much).** Utilization break-evens (~50% GPU, ~50% container duty, ~50k invocations/day) are stable and safe to apply. GPU $/hr and PaaS list prices are 2026 snapshots — treat as tunable, and prefer a live-pricing check before a binding cost claim.
- **Compliance is per-region, not per-provider**, and is always a human-confirmation gate.

## See also

- `REFERENCE.md` — the full decision matrix, 12 heuristics, cost/scale thresholds, GPU/egress/cold-start reference numbers, framework checklist, and cited sources.
- Companion skill: `deployment-audit` (audit what's already running). Companion mission: `deploy_app` (this skill feeds its preflight + cost advisor); `cost_audit` (the runtime twin).
