---
name: deployment-gcp
description: Deploy a workload to Google Cloud as a governed mission — GKE (managed k8s) or Cloud Run (serverless containers), image from Artifact Registry, DNS in Cloud DNS, Terraform state in a GCS bucket. Use when the target stack is GCP and you need the exact plan→approval→apply→configure→verify runbook, mapped to the infra operator, with real gcloud/terraform/kubectl commands and the saga rollback.
license: Apache-2.0
version: 0.1.0
---

# deployment-gcp

The provider runbook for deploying to **Google Cloud** through the `deploy_app` mission. It maps each
mission step to an infra-operator capability over Terraform + Ansible, names the real GCP resources,
and gives the exact commands. Follow it **literally** with a non-frontier driver (KIMI/Qwen) — no
tool-calling or JSON-mode required; emit each command verbatim and **stop at the approval gate**.

## When to use

The deploy spec's target stack is GCP: **GKE** (managed k8s — Standard or Autopilot) or **Cloud Run**
(serverless containers), image in **Artifact Registry**, DNS in **Cloud DNS**. If the stack is unset
or looks wrong, run `deployment-stack-selection` first — this skill executes the *how*, not the *where*.

## Prerequisites (deterministic checklist — all must exist before step 1)

1. **Credentials** — an authenticated principal (`gcloud auth list` shows an active account;
   `gcloud config get-value project` returns the target project). Prefer a service account with a
   short-lived token or Workload Identity Federation over a downloaded JSON key.
2. **Project + region** decided and set (`gcloud config set project <proj>`; region e.g.
   `us-central1`). Required APIs enabled: `container.googleapis.com`, `run.googleapis.com`,
   `artifactregistry.googleapis.com`, `dns.googleapis.com` (see `REFERENCE.md` §E).
3. **Terraform state backend** — a **GCS bucket** (versioned) already created (`gcs` backend; GCS
   provides native state locking). Backend block + bootstrap in `REFERENCE.md` §D.
4. **Artifact Registry repository** (`gcloud artifacts repositories describe <repo> --location <region>`),
   or plan to create it in the same Terraform.
5. **Terraform layout** at `deploy/terraform/envs/gcp/` and the Ansible tree at `deploy/ansible/`.
6. **kubectl / helm** (+ the `gke-gcloud-auth-plugin`) on PATH for a GKE target; app image built and
   pushed (or buildable) to Artifact Registry.

## The deployment procedure (a gated mission)

Run in this exact order. Each step is one infra-operator capability.

1. **Preflight / stack-selection sanity.** Confirm the target is GCP and the tier fits (GKE vs Cloud
   Run — see `REFERENCE.md` §A). Confirm project/region, the Artifact Registry repo, and that the app
   image is scanned (the mission's `image_scanned` step — Trivy/SBOM). Do not proceed on unknowns.
2. **`infra.plan` → `terraform plan`.** Read-only. **The diff is the evidence.**
   ```bash
   gcloud auth list
   terraform -chdir=deploy/terraform/envs/gcp init -input=false
   terraform -chdir=deploy/terraform/envs/gcp plan -input=false -no-color \
     -var project=<proj> -var region=us-central1 -var app=<app> -out=gcp.plan
   ```
   Attach the plan output to the mission as `infra_planned`.
3. **APPROVAL GATE (MANDATORY).** `infra.provision` is `approval_required=True` — the
   highest-consequence capability in the fleet. **STOP.** Present the plan diff to a human and wait
   for explicit sign-off in the cockpit inbox. **Never auto-approve.** A rejection here unwinds via the
   saga (nothing applied yet — a clean stop).
4. **`infra.provision` → `terraform apply`** (only after approval). Provisions the GCP resources:
   `google_container_cluster` + `google_container_node_pool` (or a `google_cloud_run_v2_service`),
   `google_artifact_registry_repository`, `google_dns_managed_zone` + `google_dns_record_set`, and
   (for GKE ingress) the GCLB via a `google_compute_*` address/forwarding path or a Gateway/Ingress.
   ```bash
   terraform -chdir=deploy/terraform/envs/gcp apply -input=false -no-color gcp.plan
   ```
   Undo = `infra.destroy_delta` (`terraform destroy`).
5. **`infra.configure` → `ansible-playbook`.** Pull the image from Artifact Registry, render
   manifests/compose, seed + migrate, roll out.
   ```bash
   gcloud auth configure-docker us-central1-docker.pkg.dev
   gcloud container clusters get-credentials <cluster> --region us-central1   # GKE target
   ansible-playbook deploy/ansible/playbooks/deploy-app.yml -i deploy/ansible/inventory/gcp.ini \
     -e app=<app> -e image=us-central1-docker.pkg.dev/<proj>/<repo>/<app>:<tag>
   ```
   For a **Cloud Run** target, the rollout is a `gcloud run deploy` (below).
   Undo = `infra.rollback_release` (redeploy the prior release / prior Cloud Run revision).
6. **`infra.verify` → health + smoke.** Probe `/health` and `/capabilities` on the service URL
   (Cloud Run URL or the GKE ingress host), plus one smoke request.
   ```bash
   curl -fsS https://<service-url>/health
   curl -fsS https://<service-url>/capabilities
   ```
   **A verify failure (or the gate rejection above) unwinds via the saga:** `infra.rollback_release`
   rolls back the release (or shifts traffic to the prior Cloud Run revision), then
   `infra.destroy_delta` tears down the just-applied Terraform delta — no half-applied stack.

## Provider-specific notes

- **Cloud Run one-shot deploy** (serverless, scale-to-zero):
  ```bash
  gcloud run deploy <app> --image us-central1-docker.pkg.dev/<proj>/<repo>/<app>:<tag> \
    --region us-central1 --port 8000 --no-allow-unauthenticated
  ```
  Public exposure (`--allow-unauthenticated`) is a security-posture gate — see `deployment-security`.
- **GKE auth needs `gke-gcloud-auth-plugin`.** Without it, `kubectl` errors after
  `get-credentials`. **Autopilot** removes node sizing (per-pod billing); **Standard** gives node
  pools you size.
- **Artifact Registry replaced Container Registry (gcr.io).** Use `*-docker.pkg.dev` hostnames and
  `gcloud auth configure-docker <region>-docker.pkg.dev` per region.
- **Egress + inter-region.** Internet egress ~$0.12/GB (first ~10TB), inter-region and
  cross-continent tiers higher; keep compute next to data. GKE control plane has a management fee per
  cluster/hour (one zonal cluster free per billing account on Standard).

## KIMI-reliability note (read this if you are the driving model)

- **Follow the steps in order, literally.** Do not reorder, merge, or skip.
- **Emit every command verbatim**, one per action, and wait for its result before the next.
- **STOP at step 3 (the approval gate).** Do **not** run `terraform apply` / `infra.provision`
  (or a live `gcloud run deploy` to a new service) until a human has approved. No auto-approval.
- If a step fails, **do not improvise** — report the failure and let the saga compensation
  (`infra.rollback_release` → `infra.destroy_delta`) run. Do not invent flags or resource names;
  if an exact detail is unknown, say so rather than guess.

## See also

- `REFERENCE.md` — GKE/Cloud Run tiers, machine-type/GPU sizing, egress break-evens, the full
  Terraform resource + provider reference, GCS backend bootstrap, least-privilege IAM, region caveats.
- Companion skills: `deployment-stack-selection`, `deployment-security`, `deployment-observability`,
  `deployment-audit`. Mission: `deploy_app` (this runbook is its GCP arm). Operator: `apps/infra`.
