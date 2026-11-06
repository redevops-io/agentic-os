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

**There is no default image.** The previously published `ghcr.io/redevops-io/redevops-runtime` tags `0.3.0`, `0.3.1` and
`v6` run the control plane without authentication and as root; the chart refuses them (tags and known digests), and it
refuses `latest` and every other floating tag. Until a fixed image is published and verified you must supply the
reference yourself, preferably by digest (an immutable reference), and the render fails with a `required` message when
neither `runtime.image.digest` nor `runtime.image.tag` is set. For a private package (for example a `staged-*` package on
GHCR) create an image pull secret and pass it with `imagePullSecrets`.

```bash
# Secrets first (never put them in values): the control token, and the grant-store key.
kubectl create secret generic redevops-control --from-literal=AGENTIC_OS_CONTROL_TOKEN="$(openssl rand -hex 32)"
agentic-os permissions key --env-file permissions.env --k8s-secret redevops-permissions-key   # run the kubectl command it prints

# minimal install: the image by digest, the control token and the key from Secrets
helm install rr ./deploy/reference-system/redevops-runtime \
  --set runtime.image.digest=sha256:<DIGEST OF THE VERIFIED IMAGE> \
  --set runtime.controlTokenSecret=redevops-control \
  --set permissions.keySecret=redevops-permissions-key

# private (staged) package: add a pull secret
helm install rr ./deploy/reference-system/redevops-runtime ... --set 'imagePullSecrets[0].name=ghcr-pull'

# opt-in bundled demo database (password from an existing Secret with key POSTGRES_PASSWORD; URL-safe characters only)
helm install rr ./deploy/reference-system/redevops-runtime ... \
  --set retriever.pgvector.bundled.enabled=true --set retriever.pgvector.bundled.passwordSecret=pgvector-password

# on Supermicro AMD Instinct (MI350X 8-GPU), self-contained incl. a ROCm model server (the image must be a digest you pinned):
helm install rr ./deploy/reference-system/redevops-runtime ... \
  --set model.server.enabled=true --set model.server.image=rocm/vllm@sha256:<DIGEST> \
  --set model.server.gpus=1 \
  --set 'amd.tolerations[0].key=amd.com/gpu,amd.tolerations[0].operator=Exists,amd.tolerations[0].effect=NoSchedule'

# point at a managed Postgres + an existing model endpoint (production shape; open the egress for them, see below):
helm install rr ./deploy/reference-system/redevops-runtime ... \
  --set retriever.pgvector.dsnSecret=redevops-rag-dsn \
  --set model.endpoint='http://vllm.ml-services:8000/v1' --set model.model='llama-3.1-8b' \
  --set 'networkPolicy.egress.extraRules[0].to[0].ipBlock.cidr=10.20.0.0/16'
```

When the fixed image is published and verified (signature and attestations), the default for `runtime.image.digest`
becomes that digest in a follow-up commit; until then nothing in this chart names an image tag or digest of the runtime.

## Security defaults

Every item is a default, overridable only explicitly, and enforced by `tests/test_chart_security_defaults.py` on the
rendered manifests (see `docs/release/CONTAINER_ACCEPTANCE_CRITERIA.md`):

| | default |
|---|---|
| user | non-root, uid/gid 10001 (`runtime.podSecurityContext`, `runtime.securityContext`), `fsGroup` 10001 |
| privileges | `allowPrivilegeEscalation: false`, `privileged: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault` |
| filesystem | `readOnlyRootFilesystem: true`; writable `emptyDir` mounts only at `/tmp`, `/data` (`AGENTIC_OS_HOME`), `/app/.agentic-os`, and the ledger volume at `/var/lib/redevops/ledger` (PVC when `runtime.mission.ledger.persistent`) |
| service account | `automountServiceAccountToken: false` (the runtime does not call the Kubernetes API) |
| health | `startupProbe`, `readinessProbe`, `livenessProbe` on `GET /health` |
| resources | requests and limits on every container |
| image | no default; `repo@sha256:<digest>` or a fixed version tag; `pullPolicy: IfNotPresent`; no `latest`, no vulnerable tag |
| exposure | Service `ClusterIP`; the chart has no Ingress, no LoadBalancer, no NodePort (set `runtime.service.type` explicitly if you must) |
| network | `NetworkPolicy` enabled: ingress to the runtime only from this release's verify hook and cockpit and `networkPolicy.ingress.from`; egress only DNS, the bundled pgvector / model server, and `networkPolicy.egress.extraRules` |
| secrets | `AGENTIC_OS_CONTROL_TOKEN` (`runtime.controlTokenSecret`), `PERMISSIONS_KEY` (`permissions.keySecret`), the pgvector password, the DSN (`dsnSecret`) all come from existing Secrets; no literal secret in values or templates |

**NetworkPolicy is enabled by default** because a default install needs only DNS and its own services, all allowed, and the
verify hook and cockpit are allowed in. It does not break a basic install; it does block anything you add: a
`model.endpoint`, an OTLP collector, an external Postgres DSN, a SIEM or Vault address, connected modules, and any other
namespace that should call the runtime. Add those to `networkPolicy.egress.extraRules` / `networkPolicy.ingress.from`
(`NOTES.txt` warns when it sees such endpoints). It only takes effect on a CNI that enforces NetworkPolicy. Switch off with
`networkPolicy.enabled=false` (not recommended).

The runtime needs the control token for any use beyond `/health`: without `runtime.controlTokenSecret` the mutating routes
answer 503 (fail closed) and the probes still pass.

Documented exceptions: the optional ROCm model server (`model.server.securityContext`) does not force `runAsNonRoot` or a
read-only root filesystem (GPU image whose user and cache paths are not verified); the bundled pgvector runs as the
postgres user (uid 999), not 10001. The optional `cockpit` image is user-supplied and gets the same defaults (uid 10001,
read-only root); override `cockpit.securityContext` if your image needs otherwise.

Namespace tip: label the namespace `pod-security.kubernetes.io/enforce: restricted`; the runtime, hook, cockpit and pgvector
pods are written to satisfy it (not tested against an admission controller; the model server does not satisfy `restricted`
when it runs as root).

## Upgrading from chart 0.3.x (breaking changes)

* `runtime.image.tag` no longer defaults to `0.3.1`; set `runtime.image.digest` (or a fixed `tag`). Renders without one fail.
* `security.image.tag` and `cockpit.image.tag` no longer default to `0.3.1` / `v6`; both need a `digest` or `tag`.
* `retriever.pgvector.bundled.enabled` is now `false` (it was `true` with the literal password `redevops`); when enabled it needs
  `retriever.pgvector.bundled.passwordSecret`, and `bundled.image` must be digest-pinned.
* `model.server.image` has no default (was `rocm/vllm:latest`) and must be `repo@sha256:...`.
* `deployMission.verify.image` is gone: the hook runs the runtime image (python) instead of `curlimages/curl`.
* Pods run non-root with a read-only root filesystem; an image that needs other writable paths needs more `emptyDir` mounts.
  An old root-owned data volume must be `chown`ed to 10001 once.
* `NetworkPolicy` is on (see above). New values: `runtime.controlTokenSecret`, `runtime.service.type`,
  `runtime.podSecurityContext`, `runtime.securityContext`, `runtime.probes`, `runtime.scratch`, `networkPolicy.*`,
  `retriever.pgvector.dsnSecret`, `serviceAccount.automountServiceAccountToken`, `*.image.digest`.
* Chart `version` is `0.4.0-pre.1` and there is no `appVersion`.

Not verified: no cluster install, no admission-controller run; `helm lint`, `helm template` (v3 and v4), `kubeconform` and
`trivy config` on the rendered manifests are the evidence (see the criteria document).

See `values.yaml` for the full surface. `helm lint` + `helm template` (with `runtime.image.digest` set) validate the render offline; the
post-install hook is what proves the *deployment*, and the runtime's own `verify`/`replay` prove every
*mission* thereafter.

## What this Reference System is not

It is not a second control plane, a new database, or an orchestration framework. It is packaging — Helm,
config, an acceptance gate — around a runtime whose execution semantics stay exactly where they belong. The
broader Reference System family (Terraform, Ansible, Compose, runbooks, monitoring, upgrade/rollback) lives
alongside this chart in the deploy repo; this is its Kubernetes face.
