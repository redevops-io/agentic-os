# deployment-azure — REFERENCE

Level-3 detail for the `deployment-azure` skill. Loaded when the runbook needs exact tiers, resource
names, backend/RBAC setup, or region caveats. Grounded in current (2024–2026) Azure + Terraform
primitives. Where an exact flag is version-specific, the resource/step is described rather than a
guessed flag.

## §A — Managed-vs-self-hosted tiers on Azure

| Tier | Service | Terraform resources | When it fits |
|---|---|---|---|
| Serverless containers | **Container Apps** | `azurerm_container_app`, `azurerm_container_app_environment` | Stateless HTTP/microservices, KEDA scale-to-zero, Dapr, no cluster ops |
| Managed k8s | **AKS** | `azurerm_kubernetes_cluster`, `azurerm_kubernetes_cluster_node_pool` | Full k8s, node pools, steady utilization, GPU pools |
| Registry | **ACR** | `azurerm_container_registry` | OCI images; Basic/Standard/Premium SKUs (geo-rep on Premium) |
| DNS | **Azure DNS** | `azurerm_dns_zone`, `azurerm_dns_a_record`/`_cname_record` | Managed public/private zones |
| Ingress/LB | AKS ingress / App Gateway | `azurerm_application_gateway` or ingress controller | L7; Container Apps has built-in managed ingress |
| Group | **Resource Group** | `azurerm_resource_group` | Everything lives in an RG |

Rule of thumb: **Container Apps for stateless HTTP/microservices** without running a cluster; **AKS**
when you need full k8s, GPU node pools, or ecosystem tooling. AKS free tier has no control-plane SLA;
Standard tier adds an uptime SLA fee.

## §B — VM / GPU sizing reference

| Need | VM series | Notes |
|---|---|---|
| General web/API | `B` (burstable), `D`v5 (balanced) | `Dav5`/`Dasv5` = AMD, often cheaper |
| CPU-bound | `F`v2 | compute-optimized |
| Memory-bound | `E`v5 | memory-optimized |
| GPU inference | `NC`-series (T4/A10) / `NVadsA10` | fractional A10 options for serving |
| GPU training | `ND`-series (A100/H100) | reserve/Capacity for large runs |

- **Container Apps** sizing is CPU/memory per replica (e.g. 0.25 vCPU/0.5 GiB up), with min/max
  replicas and KEDA scale rules — no VMs to size.
- **Reservations / Savings Plans** only after rightsizing (`deployment-audit`); target the stable
  baseline, not peak.

## §C — Cost / egress break-evens (Azure-specific)

- Internet egress: **~$0.087/GB** (first 100 GB/mo free, tiers down above ~10 TB).
- **Availability-zone** and **cross-region** (VNet peering, global peering) traffic bill separately —
  co-locate chatty components.
- ACR: Basic/Standard/Premium differ on storage, throughput, and geo-replication (Premium) — pick the
  SKU by image volume and multi-region need.
- Container Apps: consumption plan bills per vCPU-second + memory + requests, scale-to-zero for idle;
  dedicated plan for reserved capacity.

## §D — Terraform provider + Azure Storage state backend

Provider: **`hashicorp/azurerm`** (`provider "azurerm" { features {} }`).

Backend (in `deploy/terraform/envs/azure/backend.tf`):
```hcl
terraform {
  backend "azurerm" {
    resource_group_name  = "<org>-tfstate-rg"
    storage_account_name = "<org>tfstate"        # globally unique, 3–24 lowercase alnum
    container_name       = "tfstate"
    key                  = "envs/azure.terraform.tfstate"
  }
}
```
Bootstrap once:
```bash
az group create -n <org>-tfstate-rg -l eastus
az storage account create -n <org>tfstate -g <org>-tfstate-rg -l eastus --sku Standard_LRS
az storage container create -n tfstate --account-name <org>tfstate
```
The azurerm backend uses **blob lease locking** natively — no separate lock table.

## §E — Providers to register + least-privilege RBAC

Register the resource providers the mission uses:
```bash
az provider register --namespace Microsoft.ContainerService
az provider register --namespace Microsoft.App            # Container Apps
az provider register --namespace Microsoft.ContainerRegistry
az provider register --namespace Microsoft.Network
```

Grant only what the mission touches (prefer a dedicated service principal / managed identity scoped
to the resource group):

- **Plan (read):** `Reader` on the RG + `Storage Blob Data Contributor` on the state container.
- **Provision (write):** `Contributor` scoped to the **resource group** (not the subscription) is the
  common pragmatic grant; tighter is the specific resource roles (*Azure Kubernetes Service
  Contributor*, *AcrPush*, *DNS Zone Contributor*). Add `User Access Administrator` only if the
  Terraform also assigns roles (e.g. `az aks update --attach-acr` role bindings). Avoid `Owner` at
  subscription scope.
- **Workloads (pods):** use **Workload Identity** (federated managed identity → Kubernetes SA), not
  the node/kubelet identity, so pods get scoped permissions.
- ACR: *AcrPush* (push) / *AcrPull* (pull); attach ACR to AKS (`--attach-acr`) grants AcrPull to the
  cluster identity.

## §F — Compliance / region caveats

- **Residency/compliance is per-region, not per-provider** — pick the region first; it is a human
  gate. PHI → Azure BAA + in-scope services. EU personal data → EU region + transfer mechanism.
  **Azure Government** (sovereign cloud) for US FedRAMP High / government workloads; commercial regions
  do not qualify.
- Not every VM series / GPU / AKS feature exists in every region — verify availability (and quota) in
  the target region before planning GPU node pools. GPU quota often needs a support request.
- Public ingress (Container Apps external ingress, a public App Gateway/LB, NSG `Internet` rules) is a
  security-posture gate — see `deployment-security`.

## §G — Sources

Azure docs: AKS (node pools, AAD/Azure RBAC, attach-acr, Workload Identity, GPU node pools),
Container Apps (environments, KEDA scale, revisions, ingress), ACR (SKUs, `az acr login`,
AcrPush/AcrPull, geo-replication), Azure DNS (zones/record sets), bandwidth pricing (egress tiers,
free 100 GB), Azure Government + HIPAA/HITRUST. Terraform Registry:
`registry.terraform.io/providers/hashicorp/azurerm` (resource reference), Terraform azurerm backend
docs (blob lease locking). Verify $/GB and VM prices against live Azure pricing before a binding cost
claim.
