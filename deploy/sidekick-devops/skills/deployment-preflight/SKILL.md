---
name: deployment-preflight
description: Cloud-agnostic preflight for ANY cloud deploy — before a deploy_app mission runs, verify the driver has working credentials, the right region, sufficient permissions, a Terraform state backend, the safety/budget kill-switch, and the tools it needs (which run in the operator container, so the user only needs Docker locally). Use at the very start of any deployment (AWS/GCP/Azure/DigitalOcean/…): it produces a ✓/✗ readiness checklist with the exact fix per blocker and STOPS the mission until the hard blockers clear. The shared 80%; the per-cloud deployment-<cloud> skill supplies the CLI + Terraform specifics.
license: Apache-2.0
version: 0.1.0
---

# deployment-preflight

The **first gate of every deployment mission**, shared across all clouds. Deploying to AWS, GCP,
Azure or DigitalOcean differs only in the **CLI name** and the **Terraform provider/syntax** — the
*readiness questions are the same*. This skill owns those shared questions; the `deployment-<cloud>`
skill supplies the cloud-specific bindings (see the table below).

Run it **before** `deployment-stack-selection` acts and before `deploy_app` step 1. Emit the
checklist; **stop and ask the human** to resolve hard blockers. Never start provisioning into an
unready environment.

## The one thing the user installs: Docker

`terraform`, the cloud CLI, `ansible`, `helm`, `kubectl` all run **inside the operator container** —
Sidekick shells out to them there, not on the user's laptop. So the only hard local requirement is a
container runtime. Tell the user this up front; it removes almost all onboarding friction.

**Cross-platform install of Docker (the only must-have):**

| OS | Install |
|---|---|
| **macOS** | Docker Desktop (`brew install --cask docker`) or Colima/OrbStack |
| **Windows** | Docker Desktop (`winget install Docker.DockerDesktop`) — WSL2 backend |
| **Linux** | Docker Engine (`curl -fsSL https://get.docker.com \| sh`) or Podman |

*(Optional, only if the user wants to run commands by hand instead of via the container:)* the cloud
CLI — macOS `brew install awscli|google-cloud-sdk|azure-cli`, Windows `winget install Amazon.AWSCLI|Google.CloudSDK|Microsoft.AzureCLI`, Linux the vendor script.

## The shared checklist (all clouds)

Run each; classify **hard blocker** (deploy cannot proceed) vs **warning** (deploy proceeds, feature
degraded). The executable form is the `preflight_check` tool / `aws_demo.preflight` in the demo repo;
follow this order and map to the cloud row below.

1. **Container runtime** — `docker version` succeeds. *Hard blocker.*
2. **Credentials** — the driver has a working identity (`<cli> <whoami>` returns an account/ARN).
   Prefer a short-lived assumed role over long-lived keys. *Hard blocker.*
3. **Region / location** decided and exported. Compliance/residency is a **human gate**. *Warning if
   unset-but-defaulted; blocker if the chosen region lacks a required service.*
4. **Permissions** — probe the least-privilege set the mission needs, per role:
   - *deploy* role can create the compute/network/registry (list clusters etc.). *Hard blocker.*
   - *readonly* role can read cost + monitoring. *Warning* (cost guard degraded, deploy still runs).
   - *agent* role can reach the model/AI services. *Warning* (falls back to the existing model plane).
5. **Terraform state backend** exists (remote, versioned, locked) — or the mission will create it
   first. *Hard blocker if a remote backend is declared but missing.*
6. **Safety / budget kill-switch** — an account-level budget alarm + out-of-band destroy is armed
   **before** the first paid resource. *Hard blocker for real (non-sim) applies.*
7. **Tools present** — terraform/CLI/ansible/helm/kubectl resolve **in the operator container**
   (not the laptop). *Warning locally; blocker only if the container image is missing them.*

Emit: `READY ✓` (no hard blockers) or `BLOCKED — resolve N item(s)` with a one-line fix each.

## Per-cloud bindings (the only differences)

| Cloud | CLI | Identity probe | Deploy-perm probe | TF provider | State backend |
|---|---|---|---|---|---|
| **AWS** | `aws` | `aws sts get-caller-identity` | `aws eks list-clusters` | `hashicorp/aws` | S3 (versioned) + DynamoDB lock |
| **GCP** | `gcloud` | `gcloud auth list` / `gcloud config get project` | `gcloud container clusters list` | `hashicorp/google` | GCS bucket (versioned) |
| **Azure** | `az` | `az account show` | `az aks list` | `hashicorp/azurerm` | Storage Account + blob lease |
| **DigitalOcean** | `doctl` | `doctl account get` | `doctl kubernetes cluster list` | `digitalocean/digitalocean` | DO Spaces (S3-compatible) |

For anything cloud-specific beyond this table (resource names, IAM policy JSON, the exact
plan→apply→verify runbook), defer to `deployment-<cloud>` and `REFERENCE.md`.

## How it gates the mission

Preflight is the **opening node** of the deploy mission, not a side script:

```
deploy_app mission
  └─ node 0: preflight  ──►  READY ✓  ──► proceed to scan → plan → [approval] → provision …
                          └► BLOCKED   ──► park as a HumanTask with the checklist; do not plan/apply
```

A rejected/failed preflight never reaches `terraform plan`. Re-run after the human fixes the blockers.

## Output contract

Return a structured report the cockpit renders: `{ ready: bool, checks: [{name, status(ok|warn|fail),
detail, fix}], blockers: [...] }`. Keep each `fix` to one actionable line (a command, a console path,
or a link). See `REFERENCE.md` for the cross-platform install matrix and the executable binding.
