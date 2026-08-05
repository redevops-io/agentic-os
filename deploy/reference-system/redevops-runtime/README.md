# ReDevOps Runtime — Reference System (Helm)

A single-command, **governed** deployment of the ReDevOps runtime stack — Context Runtime + Mission
Runtime + ReDevOps RAG — packaged the way a vendor solution blueprint packages a deployable app, with two
differences that are the whole point:

1. **The deployment is itself a Mission.** The install runs a post-install/upgrade verification gate; if
   the acceptance checks fail, the release fails — the governed-deployment equivalent of a mission that
   holds instead of silently proceeding. (`templates/deploy-mission-verify.yaml`.)
2. **Execution inside the stack is deterministic.** Deny-by-default capability grants, an event-sourced
   replayable ledger, and mandatory verification are baked into the runtime config — not bolted on.

This is the "Reference System" the [Context Runtime vs. AMD Blueprints](https://redevops.io) note calls for:
it closes the *deployment-reuse* gap (Helm/K8s packaging, a one-command install, a ROCm/Instinct-ready model
server) **without** giving up *execution reuse* (planning, provenance, replay, verification stay in the
runtime).

## Relationship to a vendor blueprint (e.g. AMD Enterprise AI Agentic RAG)

| | AMD Agentic RAG blueprint | This Reference System |
|---|---|---|
| Packaging | Helm on Kubernetes, single-command, web UI | Helm on Kubernetes, single-command, optional cockpit |
| Retrieval | MCP server + ChromaDB | ReDevOps RAG (pgvector: RDS/Aurora/Cloud SQL/Azure or bundled) |
| Model | AIM microservices on Instinct/ROCm | External OpenAI-compatible endpoint **or** bundled ROCm/vLLM on Instinct |
| Deployment | `helm template` + `kubectl apply` | Governed mission — install **verified** as an Outcome, release fails on gate |
| Execution | agent + tools | deterministic, replayable, provenance-carrying missions |
| Hardware | AMD Instinct / ROCm | any Kubernetes; **runs on the same Supermicro AMD Instinct MI300X/MI350X nodes** |

**Complementary, not competing:** this stack is happy to run *on* AMD Instinct via the bundled ROCm model
server (`model.server.enabled=true`, `amd.nodeSelector`), or beside an AMD blueprint, consuming its model
endpoint. AMD standardizes deployment reuse; ReDevOps standardizes execution reuse.

## Install

```bash
# self-contained demo (bundled pgvector, no GPU, external/absent model):
helm install rr ./deploy/reference-system/redevops-runtime

# on Supermicro AMD Instinct (MI350X 8-GPU), self-contained incl. a ROCm model server:
helm install rr ./deploy/reference-system/redevops-runtime \
  --set model.server.enabled=true \
  --set model.server.gpus=1 \
  --set 'amd.tolerations[0].key=amd.com/gpu,amd.tolerations[0].operator=Exists,amd.tolerations[0].effect=NoSchedule'

# point at a managed Postgres + an existing model endpoint (production shape):
helm install rr ./deploy/reference-system/redevops-runtime \
  --set retriever.pgvector.bundled.enabled=false \
  --set retriever.pgvector.dsn='postgres://…/redevops_rag?sslmode=require' \
  --set model.endpoint='http://vllm.ml-services:8000/v1' --set model.model='llama-3.1-8b'
```

See `values.yaml` for the full surface. `helm lint` + `helm template` validate the render offline; the
post-install hook is what proves the *deployment*, and the runtime's own `verify`/`replay` prove every
*mission* thereafter.

## What this Reference System is not

It is not a second control plane, a new database, or an orchestration framework. It is packaging — Helm,
config, an acceptance gate — around a runtime whose execution semantics stay exactly where they belong. The
broader Reference System family (Terraform, Ansible, Compose, runbooks, monitoring, upgrade/rollback) lives
alongside this chart in the deploy repo; this is its Kubernetes face.
