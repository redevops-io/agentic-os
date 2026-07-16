# deployment-digitalocean — REFERENCE

Level-3 detail for the `deployment-digitalocean` skill. Loaded when the runbook needs exact tiers,
resource names, backend/token setup, or region caveats. Grounded in current (2024–2026) DigitalOcean
+ Terraform primitives. Where an exact flag is version-specific, the resource/step is described
rather than a guessed flag.

## §A — Managed-vs-self-hosted tiers on DigitalOcean

| Tier | Service | Terraform resources | When it fits |
|---|---|---|---|
| Managed PaaS | **App Platform** | `digitalocean_app` (spec) | Small stateless apps from git/DOCR; managed build, TLS, scaling |
| Managed k8s | **DOKS** | `digitalocean_kubernetes_cluster`, `digitalocean_kubernetes_node_pool` | Full k8s without control-plane ops; node pools you size |
| Registry | **DOCR** | `digitalocean_container_registry` | OCI images; `registry.digitalocean.com/<registry>` |
| Managed DB | **Managed Databases** | `digitalocean_database_cluster`, `digitalocean_database_db`, `digitalocean_database_user` | PostgreSQL/MySQL/Redis/MongoDB with backups + failover |
| DNS | **DO Domains** | `digitalocean_domain`, `digitalocean_record` | Managed DNS for domains hosted at DO |
| LB | **Load Balancer** | `digitalocean_loadbalancer` | L4/L7 in front of Droplets/DOKS services |
| Network | **VPC** | `digitalocean_vpc` | Private networking; keep DB traffic here |

Rule of thumb: **App Platform for small stateless services** (least ops), **DOKS** when you need k8s
or GPU pools. DOKS control plane is free; you pay for node Droplets + LBs + volumes.

## §B — Droplet / GPU sizing reference

| Need | Droplet class | Notes |
|---|---|---|
| General web/API | Basic (shared vCPU) | cheapest; bursty workloads |
| Steady CPU | General Purpose / CPU-Optimized | dedicated vCPU |
| Memory-bound | Memory-Optimized | caches, in-memory DBs |
| GPU | GPU Droplets (NVIDIA H100 and others, where available) | serverless-ML alternatives (Modal/RunPod) often cheaper below ~50% util |

- **App Platform** sizing is instance size + instance count per component (no Droplet to manage).
- **DOKS node pools**: pick a Droplet class per pool; autoscaling min/max on the node pool.
- DO pricing is flat and predictable (no per-request billing) — the main appeal for small teams.

## §C — Cost / egress break-evens (DO-specific)

- **Egress is pooled + cheap:** each Droplet/node includes a monthly transfer allowance pooled across
  the account; overage ~**$0.01/GB** — roughly 9× cheaper than hyperscaler internet egress. This is
  the standout reason to pick DO for bandwidth-heavy apps.
- Keep app↔database traffic on the **VPC** (private) — it does not count against transfer and avoids
  public exposure.
- Managed DB and LB are flat monthly line items; size the DB to the workload (see `deployment-audit`).

## §D — Terraform provider + Spaces state backend

Provider: **`digitalocean/digitalocean`** (`provider "digitalocean" { token = var.do_token }`).

Backend — Spaces is S3-compatible, so use the `s3` backend pointed at the Spaces endpoint (in
`deploy/terraform/envs/digitalocean/backend.tf`):
```hcl
terraform {
  backend "s3" {
    endpoints                   = { s3 = "https://nyc3.digitaloceanspaces.com" }
    bucket                      = "<org>-tfstate"
    key                         = "envs/digitalocean/terraform.tfstate"
    region                      = "us-east-1"        # placeholder; Spaces ignores AWS region
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
  }
}
```
Set `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` to the **Spaces access keys**. Bootstrap the bucket
via `doctl` or the S3 API against the Spaces endpoint.

**Locking caveat:** Spaces has no DynamoDB-equivalent lock service. Options: use Terraform ≥1.10
S3-native lockfile locking (`use_lockfile = true`) if your Spaces endpoint supports the conditional
writes it relies on, or serialize applies (single runner / CI concurrency lock), or use Terraform
Cloud/HCP for state + locking. Do not run concurrent applies against an unlocked Spaces state.

## §E — API token scopes (least privilege)

DO tokens are scoped by capability; grant only what the mission touches.

- **Plan (read):** read scope on `kubernetes`, `apps`, `registry`, `databases`, `domains`,
  `load_balancer`, plus Spaces keys with read on the state bucket.
- **Provision (write):** write scope on the specific resource types the env provisions
  (`kubernetes`, `apps`, `registry`, `databases`, `domains`, `load_balancer`, `droplet`, `vpc`).
  Avoid a full-access token where a scoped one suffices.
- **Registry:** use `doctl kubernetes cluster registry` integration to grant DOKS pull access cluster-
  wide rather than embedding registry creds in the app.
- Rotate tokens after the mission if they were minted just for it.

## §F — Compliance / region caveats

- **Residency/compliance is per-region, not per-provider** — pick the region (datacenter, e.g.
  `nyc3`, `fra1`, `sgp1`) first; it is a human gate. DO offers SOC 2 / HIPAA-eligible arrangements for
  eligible accounts — confirm eligibility and a signed BAA before placing PHI; do not assume it.
- DO's regulated-cloud coverage (FedRAMP, sovereign regions) is narrower than the hyperscalers — for
  FedRAMP High or strict sovereignty, prefer AWS GovCloud / Azure Government / a sovereign cloud
  instead. Flag this to the human at preflight.
- Not every Droplet class / GPU / managed-DB engine exists in every region — verify availability in
  the target region before planning.
- Public exposure (App Platform public route, a public Load Balancer, DB public host) is a
  security-posture gate — see `deployment-security`.

## §G — Sources

DigitalOcean docs: DOKS (node pools, registry integration, kubeconfig via doctl), App Platform (app
spec, `doctl apps`), Container Registry (`doctl registry login`, tiers), Managed Databases (VPC/
private connectivity, backups), Load Balancers, Spaces (S3-compatible API + endpoints), bandwidth/
transfer pooling + overage pricing, Terraform-on-DO tutorials. Terraform Registry:
`registry.terraform.io/providers/digitalocean/digitalocean` (resource reference), Terraform S3
backend docs (custom endpoints, ≥1.10 lockfile). Verify transfer/overage and Droplet prices against
live DO pricing before a binding cost claim.
