---
name: deployment-digitalocean
description: Deploy a workload to DigitalOcean as a governed mission — DOKS (managed k8s) or App Platform (managed PaaS), image from DOCR, DNS + a managed database, Terraform state in a Spaces (S3-compatible) bucket. Use when the target stack is DigitalOcean and you need the exact plan→approval→apply→configure→verify runbook, mapped to the infra operator, with real doctl/terraform/kubectl commands and the saga rollback.
license: Apache-2.0
version: 0.1.0
---

# deployment-digitalocean

The provider runbook for deploying to **DigitalOcean** through the `deploy_app` mission. It maps each
mission step to an infra-operator capability over Terraform + Ansible, names the real DO resources,
and gives the exact commands. Follow it **literally** with a non-frontier driver (KIMI/Qwen) — no
tool-calling or JSON-mode required; emit each command verbatim and **stop at the approval gate**.

## When to use

The deploy spec's target stack is DigitalOcean: **DOKS** (managed k8s) or **App Platform** (managed
PaaS), image in **DOCR** (Container Registry), DNS via DO domains + a **managed database**. Good fit
for small teams and predictable, low-egress bills. If the stack is unset or looks wrong, run
`deployment-stack-selection` first — this skill executes the *how*, not the *where*.

## Prerequisites (deterministic checklist — all must exist before step 1)

1. **Credentials** — a DO **API token** (`doctl auth init` succeeds; `doctl account get` returns the
   account). Scope the token to what the mission needs (see `REFERENCE.md` §E).
2. **Region** decided (e.g. `nyc3`, `fra1`). Compliance/residency is a human gate.
3. **Terraform state backend** — a **Spaces** (S3-compatible) bucket already created, using the S3
   backend with DO's Spaces endpoint. Backend block + Spaces keys in `REFERENCE.md` §D. (Spaces has no
   DynamoDB-style lock service — see the locking note there.)
4. **DOCR registry** (`doctl registry get`), or plan to create it in the same Terraform.
5. **Terraform layout** at `deploy/terraform/envs/digitalocean/` and the Ansible tree at
   `deploy/ansible/`.
6. **kubectl / helm** on PATH for a DOKS target; app image built and pushed (or buildable) to DOCR.

## The deployment procedure (a gated mission)

Run in this exact order. Each step is one infra-operator capability.

1. **Preflight / stack-selection sanity.** Confirm the target is DigitalOcean and the tier fits (DOKS
   vs App Platform — see `REFERENCE.md` §A). Confirm region, the DOCR registry, and that the app image
   is scanned (the mission's `image_scanned` step — Trivy/SBOM). Do not proceed on unknowns.
2. **`infra.plan` → `terraform plan`.** Read-only. **The diff is the evidence.**
   ```bash
   doctl account get
   terraform -chdir=deploy/terraform/envs/digitalocean init -input=false
   terraform -chdir=deploy/terraform/envs/digitalocean plan -input=false -no-color \
     -var region=nyc3 -var app=<app> -out=do.plan
   ```
   Attach the plan output to the mission as `infra_planned`.
3. **APPROVAL GATE (MANDATORY).** `infra.provision` is `approval_required=True` — the
   highest-consequence capability in the fleet. **STOP.** Present the plan diff to a human and wait
   for explicit sign-off in the cockpit inbox. **Never auto-approve.** A rejection here unwinds via the
   saga (nothing applied yet — a clean stop).
4. **`infra.provision` → `terraform apply`** (only after approval). Provisions the DO resources:
   `digitalocean_kubernetes_cluster` + `digitalocean_kubernetes_node_pool` (or a `digitalocean_app`
   App Platform spec), `digitalocean_container_registry`, `digitalocean_database_cluster` (managed
   PostgreSQL/MySQL/Redis), `digitalocean_domain` + `digitalocean_record`, and a
   `digitalocean_loadbalancer` where ingress needs one.
   ```bash
   terraform -chdir=deploy/terraform/envs/digitalocean apply -input=false -no-color do.plan
   ```
   Undo = `infra.destroy_delta` (`terraform destroy`).
5. **`infra.configure` → `ansible-playbook`.** Pull the image from DOCR, render manifests/compose,
   seed + migrate, roll out.
   ```bash
   doctl registry login
   doctl kubernetes cluster kubeconfig save <cluster>   # DOKS target
   ansible-playbook deploy/ansible/playbooks/deploy-app.yml -i deploy/ansible/inventory/do.ini \
     -e app=<app> -e image=registry.digitalocean.com/<registry>/<app>:<tag>
   ```
   For an **App Platform** target, the rollout is driven by the app spec (`doctl apps create/update`
   with a spec YAML). Undo = `infra.rollback_release` (redeploy the prior release / prior deployment).
6. **`infra.verify` → health + smoke.** Probe `/health` and `/capabilities` on the app host
   (App Platform URL or the DOKS load balancer/ingress host), plus one smoke request.
   ```bash
   curl -fsS https://<app-host>/health
   curl -fsS https://<app-host>/capabilities
   ```
   **A verify failure (or the gate rejection above) unwinds via the saga:** `infra.rollback_release`
   rolls back the release, then `infra.destroy_delta` tears down the just-applied Terraform delta —
   no half-applied stack.

## Provider-specific notes

- **DOCR pull from DOKS** is easiest via `doctl kubernetes cluster registry` integration (adds the
  pull secret cluster-wide) rather than per-deployment image pull secrets.
- **DOCR login token expires** — re-run `doctl registry login` before a fresh push/pull.
- **App Platform** is the PaaS tier: it builds/deploys from a git repo or a DOCR image via an app
  spec; scaling and TLS are managed. Use it for small stateless services; DOKS when you need k8s.
- **Managed database** (`digitalocean_database_cluster`) gives automated backups + failover; connect
  the app over the private VPC network, not the public host, to avoid egress and exposure.
- **Egress is cheap and pooled.** DO includes a monthly transfer allowance pooled across Droplets;
  overage is ~$0.01/GB — materially cheaper than hyperscaler egress. Still keep DB traffic on the VPC.

## KIMI-reliability note (read this if you are the driving model)

- **Follow the steps in order, literally.** Do not reorder, merge, or skip.
- **Emit every command verbatim**, one per action, and wait for its result before the next.
- **STOP at step 3 (the approval gate).** Do **not** run `terraform apply` / `infra.provision`
  (or a live `doctl apps create/update` to a new app) until a human has approved. No auto-approval.
- If a step fails, **do not improvise** — report the failure and let the saga compensation
  (`infra.rollback_release` → `infra.destroy_delta`) run. Do not invent flags or resource names;
  if an exact detail is unknown, say so rather than guess.

## See also

- `REFERENCE.md` — DOKS/App Platform tiers, Droplet/GPU sizing, egress break-evens, the full
  Terraform resource + provider reference, Spaces backend setup + the locking caveat, token scopes,
  region caveats.
- Companion skills: `deployment-stack-selection`, `deployment-security`, `deployment-observability`,
  `deployment-audit`. Mission: `deploy_app` (this runbook is its DigitalOcean arm). Operator: `apps/infra`.
