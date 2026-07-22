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

## Long-running monitoring + stakeholder sign-off

Deployed as a standing container (compose, or the Helm chart in your cluster), Sidekick is a
**continuous monitoring agent**: the monitor loop reads live signals (cluster usage via
metrics-server/kubectl, Prometheus, Loki) and — when a rule fires — spawns a **governed response
mission** that parks on its approval gate. Set **`ALERT_WEBHOOK_URL`** (Slack-compatible or any JSON
endpoint) and each gate immediately **alerts a stakeholder for sign-off** with a cockpit deep-link
(`COCKPIT_URL`), and each mission's outcome is alerted too — the fast path from *issue detected* to
*human approves the fix*. This is `notify.py` (an `AlertContributor` on the runtime's lifecycle);
alerting is off until the webhook is set.

```bash
ALERT_WEBHOOK_URL=https://hooks.slack.com/services/…  COCKPIT_URL=https://sidekick.internal  docker compose up
```

## Federated operators — govern external `/invoke` services

Out of the box Sidekick runs a couple of operators in-process (`infra` + `sky`). Real deployments
need more — supply-chain scanning, incident response, compliance, privacy — and those usually live
in their own repos and run as their own services. Point Sidekick at a **`modules.yaml`** (operator
name → base URL) and it governs them as first-class capabilities **without importing their code**: at
startup it discovers each service's manifest over `GET {url}/capabilities` and executes over
`POST {url}/invoke` (see [`federation.py`](federation.py) + [`modules.example.yaml`](modules.example.yaml)).

```bash
# modules.yaml maps names → in-network URLs; unreachable operators are skipped, not fatal.
SIDEKICK_MODULES=/app/modules.yaml docker compose up
```

Remote capabilities are tagged with `source: http:<operator>` provenance, so the planner's
risk-scoring never treats them as trusted-builtin. Unset `SIDEKICK_MODULES` → the built-in
in-process operators (nothing changes for the standalone bundle). A worked end-to-end example — five
deploy-and-operate operators federated behind Sidekick — is the
[`redevops-aws-demo`](https://github.com/redevops-io/redevops-aws-demo) `deploy/compose.demo.yml`.

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
