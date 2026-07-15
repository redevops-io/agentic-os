# Sidekick for DevOps — standalone bundle

A lean, one-command packaging of the Mission Runtime as a DevOps copilot: the **runtime**, the
**Projects cockpit**, the **infra operator** (deploy · teardown · drift as governed missions), the
three **deployment skills**, and a full **observability** stack (Prometheus · Grafana · Loki).
Point it at your cluster (a **read-only** kubeconfig) and your model endpoint. It only *inspects*
until you approve a gated action — nothing mutates without passing an approval gate.

## Quick start (docker compose)

```bash
cp .env.example .env          # set MODEL_ENDPOINT + KUBECONFIG_HOST
docker compose up             # builds Sidekick, starts Prometheus + Grafana + Loki + Promtail
```

- **Cockpit** — http://localhost:8000/cockpit (launch · watch · approve · EXPLAIN missions)
- **Missions API** — http://localhost:8000/missions · **Observability** — http://localhost:8000/observability
- **Grafana** — http://localhost:3000 (Prometheus + Loki datasources pre-wired)

## What's in the box

| Piece | Role |
|---|---|
| `sidekick_server.py` | the server — mounts the `/missions` API + `/cockpit` + `/observability` + the monitor loop |
| `apps/infra` operator (from the kernel) | deploy · provision · configure · verify · drift · destroy · rollback, each a gated capability |
| `skills/` | `deployment-stack-selection` · `deployment-audit` · `deployment-observability` |
| `mcp_reads.py` | read-only ops reads — cluster usage (metrics-server / kubectl), Prometheus (PromQL), Loki (LogQL) |
| `monitor.py` | the standing monitor loop — watches live signals, spawns governed response missions |
| `observability/` | Prometheus + Grafana (datasources) + Loki + Promtail configs |

## Read-only by design

- The kubeconfig is mounted **`:ro`**; cluster reads are `get`/`list` only (`kubectl top`, pod inventory).
- Every side-effecting capability (`infra.provision`, `infra.destroy_delta`, …) is **gated** — the
  mission parks on a human approval in the cockpit inbox before it runs.
- The infra operator ships with **dry-run handlers**. Wire your real runners (Terraform/Ansible/kubectl)
  by passing `run=`/`http_get=` to `build_infra_operator(...)` in `sidekick_server.py`.

## Run it in your cluster instead

See [`helm/`](helm/) for a Helm chart that runs Sidekick **in** your Kubernetes cluster with a
**read-only** ServiceAccount (pods + metrics `get`/`list`) baked in — no kubeconfig to mount.

```bash
helm install sidekick ./helm --set model.endpoint=http://my-llm:8000/v1
```

## Notes

- Pin note: the image pins `fastapi==0.115.6` — a mismatched Starlette breaks FastAPI's
  `include_router`, so don't float it.
- The bundle uses the public `agentic-os` kernel; it is not tied to any specific cloud. Configure
  `MODEL_ENDPOINT`, `PROMETHEUS_URL`, `LOKI_URL`, `GRAFANA_URL`, and `INSPECT_NAMESPACE` to taste.
