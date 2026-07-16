# deployment-gcp — REFERENCE

Level-3 detail for the `deployment-gcp` skill. Loaded when the runbook needs exact tiers, resource
names, backend/IAM setup, or region caveats. Grounded in current (2024–2026) GCP + Terraform
primitives. Where an exact flag is version-specific, the resource/step is described rather than a
guessed flag.

## §A — Managed-vs-self-hosted tiers on GCP

| Tier | Service | Terraform resources | When it fits |
|---|---|---|---|
| Serverless containers | **Cloud Run** | `google_cloud_run_v2_service`, `google_cloud_run_v2_service_iam_member` | Stateless HTTP, spiky/low duty, scale-to-zero, no cluster ops |
| Managed k8s (nodeless) | **GKE Autopilot** | `google_container_cluster` (`enable_autopilot = true`) | k8s API, per-pod billing, Google manages nodes |
| Managed k8s (nodes) | **GKE Standard** | `google_container_cluster`, `google_container_node_pool` | Node pools you size; steady utilization, GPU pools |
| Registry | **Artifact Registry** | `google_artifact_registry_repository` (format `DOCKER`) | OCI images; per-region `*-docker.pkg.dev` |
| DNS | **Cloud DNS** | `google_dns_managed_zone`, `google_dns_record_set` | Managed authoritative DNS |
| Ingress/LB | GCLB via GKE Ingress/Gateway | `google_compute_global_address`, Ingress/`Gateway` objects | L7 HTTP(S) LB; Cloud Run is fronted natively |

Rule of thumb: **Cloud Run for stateless HTTP until k8s features are needed**; GKE Autopilot removes
node ops at a per-pod premium; GKE Standard for node/GPU control at steady utilization.

## §B — Machine-type / GPU sizing reference

| Need | Machine family | Notes |
|---|---|---|
| General web/API | `e2` (cost), `n2`/`n2d` (balanced) | `n2d` = AMD, often cheaper |
| CPU-bound | `c2`/`c3` | compute-optimized |
| Memory-bound | `m1`/`m2` | large in-memory |
| GPU inference | attach `nvidia-l4` / `nvidia-tesla-t4` to a node pool | L4 for modern serving |
| GPU training | `a2` (A100), `a3` (H100) | reserve/committed-use for large runs |

- **Cloud Run** sizing is CPU (0.08–8 vCPU) + memory (128 MiB–32 GiB) per service, with `min-instances`
  to trade cold-starts for cost. Cloud Run also supports GPU (L4) for serving where available.
- **Autopilot** removes node sizing entirely (you set pod requests). **Committed Use Discounts (CUDs)**
  only after rightsizing; ~like RIs elsewhere.

## §C — Cost / egress break-evens (GCP-specific)

- Internet egress: **~$0.12/GB** for the first ~10 TB (tiers down above); inter-region and
  cross-continent are separate, higher tiers.
- **Premium vs Standard network tier** changes egress routing/price — Standard tier is cheaper for
  non-latency-critical egress.
- GKE Standard: one zonal cluster free per billing account; additional clusters carry a management
  fee per cluster-hour. Autopilot bills pod resource requests.
- Cloud Run: billed per request + CPU/memory time; scale-to-zero means no idle cost, at cold-start
  latency (~1 s typical).

## §D — Terraform provider + GCS state backend

Provider: **`hashicorp/google`** (`provider "google" { project = var.project; region = var.region }`).

Backend (in `deploy/terraform/envs/gcp/backend.tf`):
```hcl
terraform {
  backend "gcs" {
    bucket = "<org>-tfstate"
    prefix = "envs/gcp"
  }
}
```
Bootstrap once:
```bash
gcloud storage buckets create gs://<org>-tfstate --location=us-central1 --uniform-bucket-level-access
gcloud storage buckets update gs://<org>-tfstate --versioning
```
GCS provides **native state locking** — no separate lock table needed (unlike S3+DynamoDB).

## §E — Required APIs + least-privilege IAM

Enable the APIs the mission uses:
```bash
gcloud services enable container.googleapis.com run.googleapis.com \
  artifactregistry.googleapis.com dns.googleapis.com compute.googleapis.com
```

Grant only what the mission touches (prefer a dedicated deploy service account):

- **Plan (read):** `roles/viewer` scoped, or the read side of the resource roles below, plus
  `roles/storage.objectAdmin` on the state bucket.
- **Provision (write):** `roles/container.admin` (GKE), `roles/run.admin` (Cloud Run),
  `roles/artifactregistry.admin` (registry), `roles/dns.admin` (Cloud DNS), `roles/compute.admin`
  (LB/addresses), plus `roles/iam.serviceAccountUser` on the workload/runtime service account so
  Terraform can bind it. Avoid `roles/owner`.
- **Workloads (pods):** use **Workload Identity** (bind a Kubernetes SA to a Google SA), not node
  default service-account scopes, so pods get scoped permissions.
- Artifact Registry push/pull: `roles/artifactregistry.writer` (push) / `.reader` (pull) on the repo.

## §F — Compliance / region caveats

- **Residency/compliance is per-region, not per-provider** — pick the region/multi-region first; it
  is a human gate. PHI → Google Cloud BAA + covered products only. EU personal data → EU region +
  appropriate transfer mechanism. **Assured Workloads** enforces control packages (regions/personnel)
  for regulated regimes.
- Not every machine family / GPU / Autopilot feature exists in every region — verify availability in
  the target region before planning GPU pools.
- Public exposure (Cloud Run `--allow-unauthenticated`, a public GCLB) is a security-posture gate —
  see `deployment-security`.

## §G — Sources

GCP docs: GKE (Autopilot vs Standard, node pools, Workload Identity, GPU node pools), Cloud Run
(`v2` service, min-instances, GPU, IAM invoker), Artifact Registry (Docker repos, `gcloud auth
configure-docker`, gcr.io → Artifact Registry migration), Cloud DNS (managed zones/record sets),
VPC network pricing (egress tiers, Premium/Standard), Assured Workloads + HIPAA. Terraform Registry:
`registry.terraform.io/providers/hashicorp/google` (resource reference), Terraform GCS backend docs
(native locking). Verify $/GB and machine prices against live GCP pricing before a binding cost claim.
