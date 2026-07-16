---
name: deployment-azure
description: Deploy a workload to Microsoft Azure as a governed mission — AKS (managed k8s) or Container Apps (serverless containers), image from ACR, DNS in Azure DNS, Terraform state in an Azure Storage blob container. Use when the target stack is Azure and you need the exact plan→approval→apply→configure→verify runbook, mapped to the infra operator, with real az/terraform/kubectl commands and the saga rollback.
license: Apache-2.0
version: 0.1.0
---

# deployment-azure

The provider runbook for deploying to **Microsoft Azure** through the `deploy_app` mission. It maps
each mission step to an infra-operator capability over Terraform + Ansible, names the real Azure
resources, and gives the exact commands. Follow it **literally** with a non-frontier driver
(KIMI/Qwen) — no tool-calling or JSON-mode required; emit each command verbatim and **stop at the
approval gate**.

## When to use

The deploy spec's target stack is Azure: **AKS** (managed k8s) or **Container Apps** (serverless
containers), image in **ACR**, DNS in **Azure DNS**. If the stack is unset or looks wrong, run
`deployment-stack-selection` first — this skill executes the *how*, not the *where*.

## Prerequisites (deterministic checklist — all must exist before step 1)

1. **Credentials** — an authenticated principal (`az account show` returns a subscription). Prefer a
   service principal / managed identity or OIDC federation over a long-lived client secret. Least
   privilege in `REFERENCE.md` §E.
2. **Subscription + resource group + region** decided (`az group create -n <rg> -l eastus`, or an
   existing RG). Providers registered (`Microsoft.ContainerService`, `Microsoft.App`,
   `Microsoft.ContainerRegistry`, `Microsoft.Network` — see `REFERENCE.md` §E).
3. **Terraform state backend** — an **Azure Storage account** + a **blob container** already created
   (`azurerm` backend; blob lease provides native locking). Backend block + bootstrap in `REFERENCE.md` §D.
4. **ACR registry** (`az acr show -n <acr>`), or plan to create it in the same Terraform.
5. **Terraform layout** at `deploy/terraform/envs/azure/` and the Ansible tree at `deploy/ansible/`.
6. **kubectl / helm** on PATH for an AKS target; app image built and pushed (or buildable) to ACR.

## The deployment procedure (a gated mission)

Run in this exact order. Each step is one infra-operator capability.

1. **Preflight / stack-selection sanity.** Confirm the target is Azure and the tier fits (AKS vs
   Container Apps — see `REFERENCE.md` §A). Confirm subscription/RG/region, the ACR, and that the app
   image is scanned (the mission's `image_scanned` step — Trivy/SBOM). Do not proceed on unknowns.
2. **`infra.plan` → `terraform plan`.** Read-only. **The diff is the evidence.**
   ```bash
   az account show
   terraform -chdir=deploy/terraform/envs/azure init -input=false
   terraform -chdir=deploy/terraform/envs/azure plan -input=false -no-color \
     -var resource_group=<rg> -var location=eastus -var app=<app> -out=azure.plan
   ```
   Attach the plan output to the mission as `infra_planned`.
3. **APPROVAL GATE (MANDATORY).** `infra.provision` is `approval_required=True` — the
   highest-consequence capability in the fleet. **STOP.** Present the plan diff to a human and wait
   for explicit sign-off in the cockpit inbox. **Never auto-approve.** A rejection here unwinds via the
   saga (nothing applied yet — a clean stop).
4. **`infra.provision` → `terraform apply`** (only after approval). Provisions the Azure resources:
   `azurerm_resource_group` (if managed here), `azurerm_kubernetes_cluster` +
   `azurerm_kubernetes_cluster_node_pool` (or `azurerm_container_app` +
   `azurerm_container_app_environment`), `azurerm_container_registry`, `azurerm_dns_zone` +
   `azurerm_dns_a_record`.
   ```bash
   terraform -chdir=deploy/terraform/envs/azure apply -input=false -no-color azure.plan
   ```
   Undo = `infra.destroy_delta` (`terraform destroy`).
5. **`infra.configure` → `ansible-playbook`.** Pull the image from ACR, render manifests/compose,
   seed + migrate, roll out.
   ```bash
   az acr login --name <acr>
   az aks get-credentials --resource-group <rg> --name <cluster>   # AKS target
   ansible-playbook deploy/ansible/playbooks/deploy-app.yml -i deploy/ansible/inventory/azure.ini \
     -e app=<app> -e image=<acr>.azurecr.io/<app>:<tag>
   ```
   Undo = `infra.rollback_release` (redeploy the prior release / prior Container Apps revision).
6. **`infra.verify` → health + smoke.** Probe `/health` and `/capabilities` on the app host
   (Container Apps FQDN or the AKS ingress host), plus one smoke request.
   ```bash
   curl -fsS https://<app-fqdn>/health
   curl -fsS https://<app-fqdn>/capabilities
   ```
   **A verify failure (or the gate rejection above) unwinds via the saga:** `infra.rollback_release`
   rolls back the release (or activates the prior Container Apps revision), then
   `infra.destroy_delta` tears down the just-applied Terraform delta — no half-applied stack.

## Provider-specific notes

- **ACR login token is short-lived.** Re-run `az acr login --name <acr>` before a fresh push/pull;
  AKS can also pull via an attached ACR (`az aks update --attach-acr <acr>`) instead of image pull
  secrets.
- **AKS auth** uses `az aks get-credentials`; for AAD-enabled clusters the caller needs an Azure RBAC
  role (e.g. *Azure Kubernetes Service Cluster User*) or `kubectl` returns Forbidden.
- **Container Apps** (serverless, scale-to-zero) is fronted by a managed environment; ingress FQDN is
  provisioned with the app. Public ingress is a security-posture gate — see `deployment-security`.
- **Egress + zones.** Internet egress ~$0.087/GB (first 100 GB/mo free, tiers down above);
  cross-availability-zone and cross-region traffic bill separately. Keep chatty components co-located.

## KIMI-reliability note (read this if you are the driving model)

- **Follow the steps in order, literally.** Do not reorder, merge, or skip.
- **Emit every command verbatim**, one per action, and wait for its result before the next.
- **STOP at step 3 (the approval gate).** Do **not** run `terraform apply` / `infra.provision`
  until a human has approved. No auto-approval.
- If a step fails, **do not improvise** — report the failure and let the saga compensation
  (`infra.rollback_release` → `infra.destroy_delta`) run. Do not invent flags or resource names;
  if an exact detail is unknown, say so rather than guess.

## See also

- `REFERENCE.md` — AKS/Container Apps tiers, VM/GPU sizing, egress break-evens, the full Terraform
  resource + provider reference, Storage-account backend bootstrap, least-privilege RBAC, region caveats.
- Companion skills: `deployment-stack-selection`, `deployment-security`, `deployment-observability`,
  `deployment-audit`. Mission: `deploy_app` (this runbook is its Azure arm). Operator: `apps/infra`.
