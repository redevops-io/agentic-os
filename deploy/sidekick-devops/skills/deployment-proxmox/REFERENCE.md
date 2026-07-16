# deployment-proxmox — REFERENCE

Level-3 detail for the `deployment-proxmox` skill. Loaded when the runbook needs exact provider
resource names, k3s topology, registry/tunnel setup, or the self-hosting caveats. Grounded in current
(2024–2026) Proxmox VE + k3s + Terraform primitives. Where an exact attribute is provider/version
specific, the resource/step is described rather than a guessed attribute.

## §A — Self-hosted tiers on Proxmox

| Tier | What | How | When it fits |
|---|---|---|---|
| VMs | Full guests (kernel isolation) | `proxmox_vm_qemu` / `proxmox_virtual_environment_vm`, cloud-init | k3s nodes, anything needing a real kernel/GPU passthrough |
| LXC | System containers (lighter) | `proxmox_lxc` / `proxmox_virtual_environment_container` | Lightweight services; note nesting/kernel limits for k8s |
| Orchestrator | **k3s** on the VMs | Ansible `curl -sfL https://get.k3s.io \| sh -` | Homelab/on-prem Kubernetes without the full control-plane weight |
| Registry | **local** `registry:2` or **Harbor** | container on a node / LXC | Pull images on-LAN without a cloud registry |
| Ingress | **cloudflared tunnel** | `cloudflared tunnel …` | Expose services with no inbound ports / no public IP |

Rule of thumb: **run k3s on VMs, not LXC**, unless you have deliberately configured LXC for
Kubernetes (nesting, kernel modules) — VMs are the low-friction path. Single k3s server for a homelab;
HA embedded-etcd trio for anything you care about.

## §B — VM / LXC sizing + k3s topology

| Role | Sizing guide | Notes |
|---|---|---|
| k3s server (control plane) | 2 vCPU / 4 GB / 20 GB | 1 for homelab; 3 with `--cluster-init` for HA (embedded etcd) |
| k3s agent (workers) | size to workload p95 + headroom | add agents for capacity; label pools for scheduling |
| Local registry | 1 vCPU / 1–2 GB / disk = image volume | `registry:2`; Harbor if you want scanning/RBAC/replication |
| GPU node | VM with PCIe passthrough | pass the GPU through to one VM; install NVIDIA driver + container toolkit |

- k3s ships with a built-in ServiceLB (Klipper) + Traefik ingress — fine for homelab, or disable and
  bring your own. The cloudflared tunnel can target the Service directly, bypassing a public LB.
- Snapshot VMs before migrations (Proxmox backup / `vzdump`); there is no managed failover.

## §C — Cost / utilization break-even (T3 self-hosted)

- No cloud egress bill and no per-hour compute — the appeal. **But** T3 only wins at steady, high
  utilization with a named owner absorbing ops (power, cooling, hardware refresh, on-call). See
  `deployment-stack-selection` §C: DIY self-run k8s org break-even ≈ $2–2.5M/yr cloud or 50+ eng;
  a single homelab is a different calculus (sunk hardware) but the *labor* cost is the real number.
- Power/hardware amortization replaces the cloud bill — price it before claiming savings.

## §D — Terraform providers + state backend

**Two community providers exist — pick one and stay consistent:**

- **Telmate/proxmox** (`Telmate/proxmox`): resources `proxmox_vm_qemu`, `proxmox_lxc`. Long-standing,
  widely used. Provider block takes `pm_api_url`, `pm_api_token_id`, `pm_api_token_secret`.
- **bpg/proxmox** (`bpg/proxmox`): resources `proxmox_virtual_environment_vm`,
  `proxmox_virtual_environment_container`, plus datastore/file resources. More actively maintained,
  richer cloud-init/file handling. Provider block takes `endpoint` + `api_token` (or username/password).

Both clone a template and drive cloud-init (hostname, IP, SSH key, packages). Confirm the exact
attribute names against the chosen provider's registry docs — **do not mix** Telmate and bpg resource
names in one env.

State backend for `deploy/terraform/envs/proxmox/`:
- **Local backend** (`terraform.tfstate` on the runner) is acceptable for a single operator — keep it
  out of git and back it up.
- **Shared team:** point the `s3` backend at a **MinIO** (S3-compatible) endpoint (same custom-endpoint
  form as the DigitalOcean Spaces example), or use Terraform Cloud/HCP for state + locking. Local
  state has no locking — serialize applies.

## §E — Proxmox API token scopes (least privilege)

- Create a dedicated token (`user@pam!deploy`) rather than using `root@pam` where possible.
- Grant a **role** with only the privileges the mission needs: VM create/clone/config/destroy
  (`VM.Allocate`, `VM.Clone`, `VM.Config.*`, `VM.PowerMgmt`), datastore use (`Datastore.AllocateSpace`,
  `Datastore.Audit`), and pool/SDN as needed. Scope the token to the target node/pool.
- For LXC add the container-equivalent privileges. Avoid granting `Administrator`.
- Keep the token secret out of state/logs; pass via env (`PROXMOX_VE_API_TOKEN` / provider vars).

## §F — Local registry + cloudflared specifics

- **Insecure registry:** k3s uses containerd; tell every node about a plain-HTTP registry via
  `/etc/rancher/k3s/registries.yaml` (`mirrors:` + `configs:` with `insecure_skip_verify` or a CA),
  then restart k3s. Prefer TLS (Harbor or a cert on `registry:2`) for anything shared.
- **cloudflared tunnel:** `cloudflared tunnel login` (once) → `tunnel create <name>` (writes a
  credentials JSON + tunnel UUID) → `tunnel route dns <name> <host>` (CNAME) → run with a `config.yml`
  mapping `hostname` → the in-cluster service URL. Run it as a k8s Deployment (recommended, so it
  lives with the app) or a systemd service on a node. No inbound firewall rule is needed — the tunnel
  is outbound-only, which is the security win over a port-forward.

## §G — Compliance / self-hosting caveats

- **You own everything** — patching, backups, secrets, TLS, uptime. There is no managed BAA, no
  provider FedRAMP boundary. For regulated data (PHI/PCI/FedRAMP), self-hosting means *you* carry the
  full control set and audit burden — flag this to the human at preflight; a managed cloud is usually
  the right call for compliance-heavy workloads.
- Data-loss risk without a named owner is a `deployment-stack-selection` red flag — do not self-host a
  stateful workload without a backup/restore plan you have tested.
- Public exposure is via the tunnel only; do not port-forward the Proxmox host. Secrets backend and
  network policy are `deployment-security` decisions.

## §H — Sources

Proxmox VE docs (API tokens/roles, `pvesh`, cloud-init templates, `vzdump` backups). k3s docs
(quick-start `get.k3s.io`, server/agent join via `K3S_URL`/`K3S_TOKEN`, `registries.yaml`, embedded-
etcd HA `--cluster-init`, kubeconfig at `/etc/rancher/k3s/k3s.yaml`). Cloudflare docs (named tunnels,
`cloudflared tunnel create/route/run`, Kubernetes/systemd deployment, remotely-managed vs local
config). Terraform Registry: `registry.terraform.io/providers/Telmate/proxmox` and
`registry.terraform.io/providers/bpg/proxmox` (resource references — confirm attribute names there,
do not fabricate). Verify provider attribute/version specifics against the chosen provider's current
docs before applying.
