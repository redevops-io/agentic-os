# deploy-scan

The runtime-exposure scanning engine behind `apps/edge-sentinel/deploy_scan.py`. That module has shipped
for a while as a client with nowhere to call — this is the service it talks to.

Two kinds of check:

- **`web-baseline`** — nmap service-version detection against one host. Finds what's actually listening
  and reachable (open ports, the service behind each one), not a CVE database lookup. Requires the `nmap`
  binary.
- **`k8s-rbac` / `k8s-netpol` / `k8s-exposure` / `k8s-identity`** — read-only checks against a Kubernetes
  context you already hold credentials for: over-privileged RBAC bindings, namespaces with no
  NetworkPolicy, privileged/hostPath/hostNetwork pods and literal-credential env vars, and
  ServiceAccount identity over-exposure. Requires `kubectl` on PATH, configured with the context you name.

There is no cloud-IAM check (Azure Workload Identity / AWS IRSA / GCP Workload Identity) — a self-hosted
Kubernetes cluster has no cloud control plane to cross-check permissions against. `k8s-identity` covers
the equivalent self-hosted risk (ServiceAccount token over-exposure) instead.

## Authorization model — the actual enforcement

A scan only ever runs against a target you, the operator running this process, put in `SCAN_ALLOWLIST`
before starting it. There is no discovery, no credential chaining, no persistence: the process reaches
exactly what your own `nmap` network access or `kubectl` context already reaches, nothing more, and it
reaches nothing at all outside what you explicitly listed.

```
SCAN_ALLOWLIST="web-baseline=127.0.0.1:8787,k8s-rbac=my-cluster/my-namespace,k8s-netpol=my-cluster/*"
```

One `profile=target` pair per line or comma-separated. A target may end in `*` for a literal-prefix
family (`my-cluster/*` — every namespace in that one context); there is no other wildcard.

## Running it

```sh
pip install -e apps/deploy-scan
SCAN_ALLOWLIST="..." deploy-scan --port 8900
```

Then point edge-sentinel's existing client at it:

```sh
DEPLOY_SCAN_MCP_URL=http://127.0.0.1:8900/
```

Binds to loopback only by default (`--host` to change it) — this is meant to sit beside the client that
calls it, on the same host or a private network, never reachable from the internet.

## Results reach Edge Sentinel's case model

`apps/edge-sentinel/exposure_scan.py` turns one completed scan's results into an `EXPOSURE` case in
Edge Sentinel's evidence/case store — the same replayable, content-addressed evidence chain every other
case (CrowdSec, Wazuh, AI/agent security) uses. A clean scan still opens a case with no findings; that is
itself evidence, not nothing to record.

## What is NOT wired up yet

This service runs standalone — nothing in `deploy/combined` or `deploy/standalone` starts it, because it
needs the operator's own `nmap`/`kubectl` reach into infrastructure this repository has no business
running credentials for by default. Run it where your own credentials already are (a laptop, a jump
host, a CI runner with a kubeconfig mounted), and point `DEPLOY_SCAN_MCP_URL` at it from wherever Edge
Sentinel itself runs.
