---
name: deployment-proxmox
description: Deploy a workload to self-hosted Proxmox as a governed mission — Terraform provisions VMs/LXC on Proxmox, Ansible installs k3s and rolls out the app from a local registry, and a cloudflared tunnel exposes it without opening ports. Use when the target stack is on-prem/homelab Proxmox and you need the exact plan→approval→apply→configure→verify runbook, mapped to the infra operator, with real terraform/ansible/k3s/cloudflared commands and the saga rollback.
license: Apache-2.0
version: 0.1.0
---

# deployment-proxmox

The provider runbook for deploying to **self-hosted Proxmox VE** through the `deploy_app` mission. It
maps each mission step to an infra-operator capability over Terraform + Ansible, names the real
Proxmox/k3s primitives, and gives the exact commands. Follow it **literally** with a non-frontier
driver (KIMI/Qwen) — no tool-calling or JSON-mode required; emit each command verbatim and **stop at
the approval gate**.

## When to use

The deploy spec's target stack is **self-hosted**: a Proxmox VE host/cluster where the app runs on
**k3s** (lightweight Kubernetes) across VMs (or LXC), images come from a **local registry**, and
ingress is a **cloudflared tunnel** (no inbound ports opened). This is the T3 self-hosted tier from
`deployment-stack-selection` — appropriate only with a named infra owner. If the stack is unset or
looks wrong, run `deployment-stack-selection` first.

> Note: Proxmox is **not** one of the built-in cloud env dirs (`aws|gcp|azure|digitalocean`) the
> infra core ships with — you use a self-hosted env dir `deploy/terraform/envs/proxmox/` and drive it
> the same way (`terraform -chdir=…`). The mission mapping is identical.

## Prerequisites (deterministic checklist — all must exist before step 1)

1. **Proxmox access** — a Proxmox VE host reachable, with an **API token** (`root@pam!token` or a
   scoped `user@pam` token) for the Terraform provider (see `REFERENCE.md` §E). `pvesh get /version`
   works from the host.
2. **A VM template** to clone (cloud-init enabled, e.g. an Ubuntu/Debian cloud image) present on the
   node, and enough CPU/RAM/storage headroom for the planned VMs/LXC.
3. **SSH key** for Ansible into the cloned VMs; the network/bridge (e.g. `vmbr0`) and IP plan decided.
4. **Terraform state backend** — local backend is acceptable for a single-operator homelab (commit
   nothing secret), or a MinIO/S3-compatible bucket for a shared team. See `REFERENCE.md` §D.
5. **A local registry** reachable from the nodes (a `registry:2` container or Harbor), and the app
   image built + pushed to it — or plan to stand the registry up in the configure step.
6. **cloudflared** installed + a Cloudflare account/zone for the tunnel (`cloudflared tunnel login`
   done once). Terraform/Ansible layout at `deploy/terraform/envs/proxmox/` and `deploy/ansible/`.

## The deployment procedure (a gated mission)

Run in this exact order. Each step is one infra-operator capability.

1. **Preflight / stack-selection sanity.** Confirm the target is self-hosted Proxmox and that a named
   infra owner exists (T3 requires it). Confirm the template, host capacity, IP plan, the local
   registry, and that the app image is scanned (the mission's `image_scanned` step — Trivy/SBOM).
2. **`infra.plan` → `terraform plan`.** Read-only. **The diff is the evidence.**
   ```bash
   pvesh get /version
   terraform -chdir=deploy/terraform/envs/proxmox init -input=false
   terraform -chdir=deploy/terraform/envs/proxmox plan -input=false -no-color \
     -var target_node=<node> -var template=<template> -var app=<app> -out=proxmox.plan
   ```
   Attach the plan output to the mission as `infra_planned`.
3. **APPROVAL GATE (MANDATORY).** `infra.provision` is `approval_required=True` — the
   highest-consequence capability in the fleet. **STOP.** Present the plan diff to a human and wait
   for explicit sign-off. **Never auto-approve.** A rejection here unwinds via the saga (nothing
   applied yet — a clean stop).
4. **`infra.provision` → `terraform apply`** (only after approval). Provisions the VMs/LXC on Proxmox
   by cloning the template: `proxmox_vm_qemu` (VMs) or `proxmox_lxc` (containers) with the Telmate
   provider — or `proxmox_virtual_environment_vm` / `…_container` with the bpg provider (pick one;
   see `REFERENCE.md` §D). Cloud-init sets hostname/IP/SSH key.
   ```bash
   terraform -chdir=deploy/terraform/envs/proxmox apply -input=false -no-color proxmox.plan
   ```
   Undo = `infra.destroy_delta` (`terraform destroy` — deletes the just-created VMs/LXC).
5. **`infra.configure` → `ansible-playbook`.** Install **k3s**, wire the local registry, pull the
   image, render manifests, seed + migrate, roll out, and stand up the **cloudflared tunnel**.
   ```bash
   # k3s server on the first node (Ansible does this per host):
   #   curl -sfL https://get.k3s.io | sh -
   # agents join with K3S_URL + K3S_TOKEN (from /var/lib/rancher/k3s/server/node-token)
   ansible-playbook deploy/ansible/playbooks/deploy-app.yml -i deploy/ansible/inventory/proxmox.ini \
     -e app=<app> -e image=<registry-host>:5000/<app>:<tag>
   # expose without opening ports — a named tunnel to the in-cluster service:
   cloudflared tunnel create <app>
   cloudflared tunnel route dns <app> <app>.example.com
   # run the tunnel (as a k8s Deployment or a systemd service) with config.yml mapping
   # hostname -> http://<service>.<ns>.svc.cluster.local:8000
   ```
   Undo = `infra.rollback_release` (redeploy the prior release / prior manifests).
6. **`infra.verify` → health + smoke.** Probe `/health` and `/capabilities` — via the cluster service
   or the tunnel hostname — plus one smoke request.
   ```bash
   kubectl --kubeconfig deploy/ansible/artifacts/k3s.yaml get pods -A
   curl -fsS https://<app>.example.com/health
   curl -fsS https://<app>.example.com/capabilities
   ```
   **A verify failure (or the gate rejection above) unwinds via the saga:** `infra.rollback_release`
   rolls back the release, then `infra.destroy_delta` destroys the just-created VMs/LXC — no
   half-applied stack.

## Provider-specific notes

- **Registry is insecure-by-default over plain HTTP.** A `registry:2` on `:5000` needs each node's
  container runtime (k3s uses containerd) told it is insecure via `/etc/rancher/k3s/registries.yaml`,
  or front it with TLS. Ansible should render `registries.yaml` before the rollout.
- **cloudflared tunnel = no inbound ports.** The tunnel dials *out* to Cloudflare, so you never expose
  the Proxmox host or open a firewall port — the safest ingress for a homelab. DNS is a CNAME to the
  tunnel. (This replaces a cloud load balancer / public IP.)
- **k3s is single-binary k8s.** One server node is fine for a homelab; add agents for capacity, or an
  embedded-etcd HA trio (`--cluster-init`) for resilience. `kubeconfig` is at
  `/etc/rancher/k3s/k3s.yaml` on the server (copy it out, rewrite the server URL to the node IP).
- **No cloud egress bill, but real power/cooling/uptime cost.** T3 economics only beat managed cloud
  at steady high utilization with an owner absorbing ops — see `deployment-stack-selection` §C.
- **Backups matter more here.** No managed-DB failover; snapshot VMs (Proxmox backup) and back up the
  app's data volume before migrations.

## KIMI-reliability note (read this if you are the driving model)

- **Follow the steps in order, literally.** Do not reorder, merge, or skip.
- **Emit every command verbatim**, one per action, and wait for its result before the next.
- **STOP at step 3 (the approval gate).** Do **not** run `terraform apply` / `infra.provision`
  until a human has approved. No auto-approval.
- If a step fails, **do not improvise** — report the failure and let the saga compensation
  (`infra.rollback_release` → `infra.destroy_delta`) run. Do not invent Proxmox provider attributes
  or k3s flags; if an exact detail is unknown, say so rather than guess.

## See also

- `REFERENCE.md` — the two Proxmox Terraform providers (Telmate vs bpg) and their resource names,
  VM/LXC sizing, k3s topology, local-registry + cloudflared setup, state-backend options, API-token
  scopes, and the self-hosting caveats.
- Companion skills: `deployment-stack-selection` (T3 fit + break-evens), `deployment-security`,
  `deployment-observability`, `deployment-audit`. Mission: `deploy_app` (this runbook is its
  self-hosted arm). Operator: `apps/infra`.
